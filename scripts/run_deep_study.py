"""Run deterministic KV/execution studies and bounded paired profiler captures."""

from __future__ import annotations

import argparse
import concurrent.futures
import inspect
import json
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from scripts.build_deep_trace_patches import build
from scripts.run_source_study import selected_command
from src.common import read_json, write_json, write_text
from src.deep_study_contract import load_config, make_jobs, override_command
from src.openai_stream import iter_openai_events, stream_sse
from src.parse_execution_trace import parse_events as parse_execution
from src.parse_kv_trace import parse_events as parse_kv
from src.parse_scheduler_trace import load_events
from src.profile_tables import paired_metrics, write_csv
from src.study_contract import VLLM_COMMIT, fingerprint
from src.study_runner import fetch_text, load_tokenizer, managed_server, request_record
from src.summarize_nsys import write_summary as summarize_nsys
from src.summarize_torch_profile import write_summary as summarize_torch


def verify_source(checkout, week):
    root = Path(checkout).resolve()
    with tempfile.TemporaryDirectory(prefix="deep-source-check-") as temporary:
        week9, week10 = build(root, temporary)
    expected = week9 if week == 9 else week10
    for name, content in expected.items():
        if not (root / name).is_file() or (root / name).read_text() != content:
            raise ValueError(f"apply the reviewed Week {min(week, 10)} patch chain: {name}")
    changed = subprocess.check_output(
        ["git", "-C", str(root), "diff", "--name-only", "HEAD"], text=True
    ).splitlines()
    untracked = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"], text=True
    ).splitlines()
    if (
        set(changed) | {p for p in untracked if p.startswith("vllm/") and p.endswith(".py")}
    ) - expected.keys():
        raise ValueError("unreviewed vLLM source changes")
    import vllm

    if Path(inspect.getfile(vllm)).resolve().parent != root / "vllm":
        raise ValueError("Python must import the patched editable vLLM checkout")
    return dict(
        commit=VLLM_COMMIT,
        imported_package=inspect.getfile(vllm),
        patch_week=min(week, 10),
        source_fingerprint=fingerprint(expected),
    )


def abort_request(url, model, job, timeout, seed):
    payload = dict(
        model=model,
        prompt=job["prompt_ids"],
        max_tokens=job["output_tokens"],
        request_id=job["request_id"],
        temperature=0,
        seed=seed,
        ignore_eos=True,
    )
    stream = iter_openai_events(
        stream_sse(url + "/v1/completions", payload, timeout=timeout, max_duration=timeout)
    )
    try:
        for event in stream:
            if event.content:
                return dict(
                    request_id=job["request_id"],
                    status="client_disconnected",
                    closed_ns=time.monotonic_ns(),
                )
        raise ValueError("abort request ended before receiving content")
    finally:
        stream.close()


def send_jobs(url, base, jobs, config, *, sequential=False):
    model, timeout, seed = (
        base["model"]["served_model_name"],
        config["timeout_seconds"],
        config["seed"],
    )
    if sequential:
        records = []
        for job in jobs:
            if job["abort"]:
                records.append(abort_request(url, model, job, timeout, seed))
            else:
                records.append(
                    request_record(url, model, job, time.time(), time.monotonic(), timeout, seed)
                )
        return records
    wall, mono = time.time(), time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = []
        for job in jobs:
            time.sleep(max(0, mono + job["offset"] - time.monotonic()))
            futures.append(pool.submit(request_record, url, model, job, wall, mono, timeout, seed))
        return [f.result() for f in futures]


def require_clients(records):
    if any(r["status"] not in {"success", "client_disconnected"} for r in records):
        raise ValueError("client workload failed; inspect client.json")


def wait_kv(root, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return parse_kv(load_events(root.glob("kv-*.jsonl")))
        except ValueError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def nsys_prefix(config, root):
    cfg = config["nsys"]
    binary = shutil.which(cfg["binary"])
    if binary is None:
        raise ValueError("nsys is missing; install Nsight Systems on the GPU VM before --run")
    write_text(
        root / "nsys-version.txt",
        subprocess.check_output([binary, "--version"], text=True, timeout=30),
    )
    help_text = subprocess.check_output([binary, "profile", "--help"], text=True, timeout=30)
    write_text(root / "nsys-profile-help.txt", help_text)
    if not all(
        flag in help_text
        for flag in ("capture-range-end", "cudaProfilerApi", "trace-fork-before-exec")
    ):
        raise ValueError("installed nsys lacks the required capture-range/fork options")
    return [
        binary,
        "profile",
        "--trace=" + ",".join(cfg["domains"]),
        "--sample=none",
        "--cpuctxsw=none",
        "--trace-fork-before-exec=true",
        "--capture-range=cudaProfilerApi",
        "--capture-range-end=stop",
        "--force-overwrite=false",
        "--wait=all",
        "--output=" + str((root / "profile").resolve()),
    ]


def run_one(config, base, argv, scenario, jobs, root, mode):
    root.mkdir(parents=True, exist_ok=False)
    env, prefix = {}, None
    if mode == "kv":
        env = {
            "VLLM_KV_TRACE_DIR": str(root.resolve()),
            "VLLM_KV_TRACE_LIMIT": str(config["event_limit"]),
        }
    elif mode != "baseline":
        capture = {
            **scenario.get("capture", {}),
            **config.get("profiler", {}),
            "mode": mode,
            "output": str(root.resolve()),
            "arm_file": str((root / "armed").resolve()),
            "event_limit": config["event_limit"],
        }
        env = {"VLLM_EXECUTION_CONFIG": json.dumps(capture)}
        write_json(root / "instrumentation.json", capture)
        if mode == "nsys":
            prefix = nsys_prefix(config, root)
    analysis = None
    with managed_server(
        base,
        argv,
        root,
        env=env,
        source_study=True,
        launch_prefix=prefix,
        shutdown_signal=signal.SIGINT if prefix else signal.SIGTERM,
    ) as (url, _):
        write_text(root / "metrics-before.txt", fetch_text(url + "/metrics"))
        if config["week"] >= 11:
            for i in range(config["warmup_requests"]):
                warmup = send_jobs(url, base, jobs, config)
                write_json(root / f"warmup-{i}.json", warmup)
                require_clients(warmup)
            if mode != "baseline":
                (root / "armed").touch(exist_ok=False)
        records = send_jobs(url, base, jobs, config, sequential=mode == "kv")
        write_json(root / "client.json", records)
        require_clients(records)
        if mode == "kv":
            analysis = wait_kv(root, config["timeout_seconds"])
        elif mode != "baseline":
            deadline = time.monotonic() + config["timeout_seconds"]
            while mode in {"torch", "nsys"} and not list(root.glob("capture-*.json")):
                if time.monotonic() >= deadline:
                    raise ValueError("capture window was not completed by this workload")
                time.sleep(0.2)
            analysis = parse_execution(load_events(root.glob("execution-*.jsonl")))
        write_text(root / "metrics-after.txt", fetch_text(url + "/metrics"))
    if analysis is not None:
        write_json(root / "analysis.json", analysis)
        write_csv(
            root / "shapes.csv" if mode != "kv" else root / "request-block-timeline.csv",
            analysis["steps"] if mode != "kv" else analysis["request_timeline"],
        )
    return records, analysis


def observations(name, scenario, analysis, capture=None):
    if "prefix_lookups" in analysis:
        hit = any(e["cached_tokens"] > 0 for e in analysis["prefix_lookups"])
        aborted = any(
            e["finish_reason"] == "FINISHED_ABORTED" for e in analysis["request_timeline"]
        )
        seen = {
            "cold": not hit,
            "exact": hit,
            "partial": hit,
            "pressure": analysis["evictions"] > 0,
            "abort": aborted,
        }[name]
    else:
        rows = analysis["steps"]
        if capture:
            rows = [r for r in rows if r["step"] in capture["active_steps"]]
        target = "mixed" if "mixed" in name else "prefill" if "prefill" in name else "decode"
        seen = any(r["composition"] == target for r in rows)
        if target == "decode":
            seen = seen and all(r["composition"] == "decode" for r in rows) if capture else seen
        if "graph" in name:
            seen = seen and any(r["execution_mode"] == "graph_replay" for r in rows)
        if scenario["overrides"].get("enforce_eager"):
            seen = seen and all(r["execution_mode"] == "eager" for r in rows)
    return dict(
        target_observed=seen,
        verdict="observed" if seen else "not_observed",
        explanation=None if seen else "inspect raw evidence; adjust one variable in a new session",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--scenario")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--plan", action="store_true")
    modes.add_argument("--run", action="store_true")
    args = parser.parse_args()
    config, source_config, base, scenarios = load_config(args.config)
    if args.scenario:
        scenarios = {args.scenario: scenarios[args.scenario]}
    argv = selected_command(source_config, base, execution=args.run)
    commands = {name: override_command(argv, s["overrides"]) for name, s in scenarios.items()}
    if args.plan:
        print(
            json.dumps(
                dict(
                    week=config["week"],
                    source=source_config["source_checkout"],
                    commit=VLLM_COMMIT,
                    operating_point=source_config["operating_point"],
                    scenarios=scenarios,
                    server_commands=commands,
                    evidence="plan only; no CUDA execution",
                ),
                indent=2,
            )
        )
        return
    for forbidden in (
        "--speculative-config",
        "--enable-lora",
        "--async-scheduling",
        "--kv-transfer-config",
    ):
        if any(a == forbidden or a.startswith(forbidden + "=") for a in argv):
            raise ValueError(f"unsupported source-study path: {forbidden}")
    source = verify_source(source_config["source_checkout"], config["week"])
    tokenizer = load_tokenizer(base)
    for name, scenario in scenarios.items():
        category = {9: "traces", 10: "traces", 11: "profiles", 12: "nsys"}[config["week"]]
        root = Path(config["output_root"]) / category / name / str(uuid.uuid4())
        root.mkdir(parents=True, exist_ok=False)
        jobs = make_jobs(scenario, tokenizer, config["seed"])
        metadata = dict(
            config=config,
            scenario=scenario,
            config_fingerprint=fingerprint(config),
            source=source,
            workload_id=fingerprint(jobs),
            status="running",
            server_argv=commands[name],
            tokenizer=base["model"],
            jobs=[
                {k: v for k, v in j.items() if k != "prompt_ids"}
                | {"prompt_sha256": fingerprint(j["prompt_ids"])}
                for j in jobs
            ],
        )
        write_json(root / "run.json", metadata)
        try:
            mode = {9: "kv", 10: "execution", 11: "torch", 12: "nsys"}[config["week"]]
            if config["week"] >= 11:
                baseline, _ = run_one(
                    config, base, commands[name], scenario, jobs, root / "baseline", "baseline"
                )
            records, analysis = run_one(
                config, base, commands[name], scenario, jobs, root / "capture", mode
            )
            capture = None
            if config["week"] >= 11:
                manifests = list((root / "capture").glob("capture-*.json"))
                if len(manifests) != 1:
                    raise ValueError("expected one completed worker capture")
                capture = read_json(manifests[0])
                a, b = [read_json(root / p / "server.json") for p in ("baseline", "capture")]
                if a["runtime"] != b["runtime"] or a["argv"] != b["argv"]:
                    raise ValueError("baseline/profile runtime or command drift")
                write_json(root / "pair.json", paired_metrics(baseline, records))
                tables = Path(config["output_root"]) / "tables" / name / root.name
                if mode == "torch":
                    traces = list((root / "capture").glob("torch-*.json"))
                    operators = list((root / "capture").glob("operators-*.json"))
                    if len(traces) != 1 or len(operators) != 1:
                        raise ValueError("expected one torch trace/operator pair")
                    summarize_torch(traces[0], operators[0], tables)
                else:
                    report = root / "capture" / "profile.nsys-rep"
                    if not report.is_file():
                        raise ValueError("nsys did not flush profile.nsys-rep; inspect server.log")
                    sqlite_path = root / "capture" / "profile.sqlite"
                    export_argv = [
                        config["nsys"]["binary"],
                        "export",
                        "--type=sqlite",
                        "--output=" + str(sqlite_path),
                        str(report),
                    ]
                    write_json(root / "export-command.json", export_argv)
                    subprocess.run(
                        export_argv, check=True, timeout=config["nsys"]["export_timeout_seconds"]
                    )
                    summarize_nsys(
                        sqlite_path,
                        tables,
                        Path(config["output_root"]) / "figures" / name / f"{root.name}.svg",
                    )
            write_json(root / "observation.json", observations(name, scenario, analysis, capture))
            metadata["status"] = "collected"
        except BaseException as error:
            metadata.update(status="failed", error_type=type(error).__name__, error=str(error))
            raise
        finally:
            write_json(root / "run.json", metadata)
        print(f"{name}: {root}")


if __name__ == "__main__":
    main()
