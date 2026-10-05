"""Plan/replay Week 13 prefix and Week 14 tuning/TP matrices without implicit GPU launch."""

from __future__ import annotations

import argparse
import copy
import importlib.metadata
import json
import os
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

from src.common import load_yaml, source_identity, write_json, write_text
from src.experiment_client import request, run_load
from src.serving_experiment import counter_delta, engine_command, load_experiment, summarize, trace_jobs
from src.study_contract import fingerprint
from src.study_runner import GPUSampler, fetch_text, load_tokenizer, managed_server


def gpu_identity(devices, product):
    gpu = subprocess.check_output([
        "nvidia-smi", "-i", ",".join(map(str, devices)),
        "--query-gpu=name,uuid,driver_version,memory.total", "--format=csv,noheader,nounits"],
        text=True, timeout=15).strip()
    if len(gpu.splitlines()) != len(devices) or any(product.replace("-", " ") not in line.replace("-", " ") for line in gpu.splitlines()):
        raise ValueError("allocated GPU count/product does not match the controlled experiment")
    topology = subprocess.check_output(["nvidia-smi", "topo", "-m"], text=True, timeout=15)
    freeze = subprocess.check_output([os.sys.executable, "-m", "pip", "freeze", "--all"], text=True, timeout=30)
    return dict(hostname=socket.gethostname(), devices=devices, gpu=gpu, topology=topology,
                vllm_version=importlib.metadata.version("vllm"), pip_freeze=freeze)


def local_server_base(base):
    return dict(model=base["model"], server=dict(host="127.0.0.1", port=8000,
                startup_timeout_seconds=600, shutdown_timeout_seconds=30, readiness_poll_seconds=1))


def metric_snapshot(url, root, name):
    value = fetch_text(url + "/metrics")
    write_text(root / name, value)
    return value


class MetricSeries:
    """Keep raw cache/queue/preemption series for the cold-to-warm transition."""
    def __init__(self, url, root):
        self.url, self.root = url, root
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)

    def sample(self):
        with (self.root / "metric-series.jsonl").open("x") as journal:
            while not self.stop.is_set():
                row = {"timestamp": time.time()}
                try:
                    row["exposition"] = fetch_text(self.url + "/metrics")
                except Exception as error:
                    row["error_type"] = type(error).__name__
                journal.write(json.dumps(row) + "\n")
                journal.flush()
                self.stop.wait(1)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=12)


def run_cell(cfg, base, case_name, workload, rate_name, repeat, tokenizer, session):
    case = cfg["cases"][case_name]
    root = session / f"r{repeat}-{rate_name}-{workload}-{case_name}"
    root.mkdir()
    rate = cfg["rates"][rate_name] or base.get("near_slo_rps")
    jobs = trace_jobs(cfg, tokenizer, workload, rate, repeat, low_overlap=case["cache"] == "low_overlap")
    warmup = trace_jobs(cfg, tokenizer, "short", rate, repeat, warmup=True)
    argv = engine_command(base, case.get("overrides"))
    tp = case.get("overrides", {}).get("tensor_parallel_size", base["gpus_per_replica"])
    devices = case.get("devices", list(range(tp)))
    if len(devices) != tp or len(set(devices)) != tp:
        raise ValueError("visible GPU allocation must equal TP")
    # Identical trace within each repetition, independent arrivals between repetitions.
    write_json(root / "trace.json", jobs)
    meta = dict(status="running", case=case_name, repeat=repeat, workload=workload,
                rate_name=rate_name, offered_rps=rate, config=cfg, baseline=base,
                workload_id=fingerprint(jobs), server_argv=argv, source=source_identity(),
                cache_initialization="fresh_process", profiler=False, allocated_gpus=tp)
    write_json(root / "run.json", meta)
    begin = time.monotonic()
    try:
        with managed_server(local_server_base(base), argv, root,
                            env={"CUDA_VISIBLE_DEVICES": ",".join(map(str, devices))},
                            runtime_probe=lambda: gpu_identity(devices, base["gpu_product"]),
                            source_study=True) as (url, server):
            meta["startup_seconds"] = server["startup_seconds"]
            model = base["model"]["served_model_name"]
            warm_records = []
            for job in warmup:
                warm_records.append(request(url + "/v1/completions", model, {**job, "offset": 0},
                                            time.time(), time.monotonic(), cfg["timeout_seconds"], cfg["seed"]))
            write_json(root / "model-warmup.json", warm_records)
            if any(r["status"] != "success" for r in warm_records):
                raise ValueError("model warmup failed")
            priming = []
            start = time.monotonic()
            if case["cache"] == "warm":
                for job in jobs[:cfg["prefix_families"]]:
                    prime = {**job, "prompt_ids": job["prompt_ids"][:cfg["shared_tokens"]], "output_tokens": 1, "offset": 0}
                    priming.append(request(url + "/v1/completions", model, prime,
                                           time.time(), time.monotonic(), cfg["timeout_seconds"], cfg["seed"]))
            write_json(root / "prefix-warmup.json", priming)
            meta.update(prefix_warmup_seconds=time.monotonic() - start,
                        prefix_warmup_tokens=sum(r["prompt_tokens"] + r["output_tokens"] for r in priming))
            if any(r["status"] != "success" for r in priming):
                raise ValueError("prefix warmup failed")
            before = metric_snapshot(url, root, "metrics-before.txt")
            with GPUSampler(root / "gpu.csv", 1), MetricSeries(url, root):
                rows, window = run_load(url + "/v1/completions", model, jobs, cfg, root / "client.jsonl")
            after = metric_snapshot(url, root, "metrics-after.txt")
            summary = summarize(rows, base["slo"], window)
            summary["by_workload"] = {kind: summarize([r for r in rows if r["workload"] == kind], base["slo"], window) for kind in {r["workload"] for r in rows}}
            metric = cfg.get("cache_metrics")
            if metric:
                summary["cache"] = {k: counter_delta(before, after, metric[k]) for k in ("hits", "queries")}
                summary["cache"].update(unit=metric["unit"], definition=metric["definition"],
                                       interpretation="unverified" if not metric["definition"] else "definition supplied")
            summary["eligible"] = (summary["error_rate"] <= base["slo"]["max_error_rate"]
                and summary["arrival_lag_ms_p99"] is not None
                and summary["arrival_lag_ms_p99"] <= base["max_arrival_lag_ms"])
            write_json(root / "summary.json", summary)
            meta["status"] = "collected" if summary["eligible"] else "rejected"
    except BaseException as error:
        meta.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        meta["allocated_gpu_seconds"] = tp * (time.monotonic() - begin)
        meta["cost_boundary"] = "server lifecycle only; VM idle/disk billing must be added"
        write_json(root / "run.json", meta)
    return meta


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--case")
    parser.add_argument("--workload")
    parser.add_argument("--rate")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--run", action="store_true")
    args = parser.parse_args()
    cfg, base = load_experiment(args.config)
    cfg = copy.deepcopy(cfg)
    if args.case:
        cfg["cases"] = {args.case: cfg["cases"][args.case]}
    if args.workload:
        if args.workload not in cfg["workloads"]:
            parser.error("unknown workload")
        cfg["workloads"] = [args.workload]
    if args.rate:
        cfg["rates"] = {args.rate: cfg["rates"][args.rate]}
    cells = []
    for repeat in range(cfg["repeats"]):
        names = list(cfg["cases"])
        # Rotate candidate order across repeats to reduce monotonic clock/temperature bias.
        names = names[repeat % len(names):] + names[:repeat % len(names)]
        for rate in cfg["rates"]:
            for workload in cfg["workloads"]:
                cells.extend((name, workload, rate, repeat) for name in names)
    commands = {name: engine_command(base, case.get("overrides")) for name, case in cfg["cases"].items()}
    if args.plan:
        print(json.dumps(dict(cells=cells, commands=commands, hardware_executed=False,
                              unresolved_rates=[k for k, v in cfg["rates"].items() if not (v or base.get("near_slo_rps"))]), indent=2))
        return
    if any(not (v or base.get("near_slo_rps")) for v in cfg["rates"].values()):
        raise ValueError("freeze near_slo_rps from measured evidence or select --rate low")
    from scripts.run_deep_study import verify_source
    source = load_yaml(base["source_config"])
    verify_source(source["source_checkout"], 10)
    tokenizer = load_tokenizer(base)
    session = Path(cfg["output_root"]) / str(uuid.uuid4())
    session.mkdir(parents=True)
    results = []
    for cell in cells:
        results.append(run_cell(cfg, base, *cell, tokenizer, session))
    write_json(session / "matrix.json", results)
    print(session)
    if any(r["status"] != "collected" for r in results):
        raise SystemExit("one or more cells were rejected; inspect raw failures")


if __name__ == "__main__":
    main()
