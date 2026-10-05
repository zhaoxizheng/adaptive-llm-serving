"""One bounded kernel counter capture under the reviewed Week 10 worker instrumentation."""

import argparse
import json
import signal
import subprocess
import time
import uuid
from pathlib import Path

from src.common import load_yaml, source_identity, write_json, write_text
from src.experiment_client import request
from src.profile_tables import write_csv
from src.serving_experiment import engine_command, validate_baseline
from src.study_runner import load_tokenizer, managed_server
from src.summarize_ncu import parse_counters


def ncu_command(cfg, root):
    ncu, target = cfg["ncu"], cfg["target"]
    if ncu["launch_count"] < 1 or ncu["launch_count"] > 16 or ncu["launch_skip"] < 0:
        raise ValueError("counter capture must select 1-16 launches")
    if ncu["replay_mode"] not in {"kernel", "application"}:
        raise ValueError("unsupported replay mode")
    if ncu["replay_mode"] == "application":
        raise ValueError("application replay cannot reproduce an externally driven server; use kernel replay")
    argv = [ncu["binary"], "--target-processes", "all", "--profile-from-start", "off",
            "--kernel-name", "regex:" + str(target["kernel_regex"]),
            "--replay-mode", ncu["replay_mode"], "--cache-control", ncu["cache_control"],
            "--clock-control", ncu["clock_control"], "--launch-skip", str(ncu["launch_skip"]),
            "--launch-count", str(ncu["launch_count"]), "--export", str(root / "profile")]
    if target.get("nvtx_include"):
        argv += ["--nvtx", "--nvtx-include", target["nvtx_include"]]
    for section in ncu["sections"]:
        argv += ["--section", section]
    return argv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/week13-kernel.yaml")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--plan", action="store_true")
    group.add_argument("--run", action="store_true")
    args = parser.parse_args()
    cfg = load_yaml(args.config)
    base = validate_baseline(load_yaml(cfg["baseline"]))
    preview = ncu_command(cfg, Path("SESSION"))
    if args.plan:
        print(json.dumps(dict(command=preview, target=cfg["target"], hardware_executed=False), indent=2))
        return
    if not all(cfg["target"].get(k) for k in ("week12_evidence", "hypothesis", "kernel_regex", "shape")):
        raise ValueError("fill the Week 12 kernel target, shape, hypothesis and evidence before capture")
    from scripts.run_deep_study import verify_source
    source = load_yaml(base["source_config"])
    verify_source(source["source_checkout"], 10)
    root = (Path(cfg["output_root"]) / str(uuid.uuid4())).resolve()
    root.mkdir(parents=True)
    meta = dict(status="running", config=cfg, baseline=base, source=source_identity(),
                purpose="mechanism only; never use ncu latency as service capacity")
    write_json(root / "run.json", meta)
    try:
        binary = cfg["ncu"]["binary"]
        for label, extra in (("version", ["--version"]), ("help", ["--help"]),
                             ("sections", ["--list-sections"]), ("metrics", ["--query-metrics"])):
            text = subprocess.check_output([binary, *extra], text=True, timeout=60)
            write_text(root / f"ncu-{label}.txt", text)
        sections = (root / "ncu-sections.txt").read_text()
        if any(section not in sections for section in cfg["ncu"]["sections"]):
            raise ValueError("requested ncu section unavailable on this installation")
        tokenizer = load_tokenizer(base)
        vocab = sorted(set(tokenizer.get_vocab().values()) - set(tokenizer.all_special_ids))
        shape = cfg["workload"]
        job = dict(request_id="kernel-target", workload="kernel", offset=0,
                   prompt_ids=[vocab[0]] * shape["prompt_tokens"], output_tokens=shape["output_tokens"])
        capture = dict(mode="nsys", wait=0, warmup=0, active=1, output=str(root),
                       arm_file=str(root / "armed"), event_limit=10000)
        argv = engine_command(base)
        prefix = ncu_command(cfg, root)
        write_json(root / "command.json", prefix + argv)
        from scripts.run_serving_experiment import local_server_base
        runtime = local_server_base(base)
        runtime["server"]["startup_timeout_seconds"] = cfg["ncu"]["timeout_seconds"]
        with managed_server(runtime, argv, root, source_study=True, launch_prefix=prefix,
                            shutdown_signal=signal.SIGINT,
                            env={"VLLM_EXECUTION_CONFIG": json.dumps(capture)}) as (url, _):
            model = base["model"]["served_model_name"]
            warm = request(url + "/v1/completions", model, job, time.time(), time.monotonic(), cfg["ncu"]["timeout_seconds"], 42)
            write_json(root / "warmup.json", warm)
            if warm["status"] != "success":
                raise ValueError("counter warmup failed")
            (root / "armed").touch()
            row = request(url + "/v1/completions", model, job, time.time(), time.monotonic(), cfg["ncu"]["timeout_seconds"], 42)
            write_json(root / "client.json", row)
            if row["status"] != "success" or not list(root.glob("capture-*.json")):
                raise ValueError("counter capture window incomplete")
        report = root / "profile.ncu-rep"
        if not report.is_file():
            raise ValueError("no ncu report; inspect kernel filter and counter permissions")
        export = [binary, "--import", str(report), "--page", "raw", "--csv"]
        write_json(root / "export-command.json", export)
        text = subprocess.check_output(export, text=True, timeout=60)
        write_text(root / "raw.csv", text)
        write_csv(root / "counters.csv", parse_counters(text))
        meta["status"] = "collected"
    except BaseException as error:
        log = (root / "server.log").read_text() if (root / "server.log").exists() else ""
        meta.update(status="blocked_counter_permission" if "ERR_NVGPUCTRPERM" in log else "failed",
                    error_type=type(error).__name__, error=str(error))
        raise
    finally:
        write_json(root / "run.json", meta)
    print(root)


if __name__ == "__main__":
    main()
