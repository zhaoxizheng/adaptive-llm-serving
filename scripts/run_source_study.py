"""Execute the pinned request-lifecycle or deterministic scheduler trace study."""

from __future__ import annotations

import argparse
import concurrent.futures
import inspect
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from scripts.build_trace_patches import build
from src.common import load_yaml, read_json, write_json, write_text
from src.openai_stream import OpenAIHTTPError, http_json, iter_openai_events, stream_sse
from src.parse_request_trace import sequence_diagram, validate_path
from src.parse_scheduler_trace import draw, load_events, parse_events, write_tables
from src.study_contract import VLLM_COMMIT, load_study, serve_command
from src.study_runner import fetch_text, load_tokenizer, managed_server, request_record


def verify_source(checkout, week):
    with tempfile.TemporaryDirectory(prefix="vllm-trace-check-") as temporary:
        week7, week8 = build(checkout, temporary)
    expected = week8 if week == 8 else week7
    root = Path(checkout).resolve()
    for name, content in expected.items():
        if not (root / name).is_file() or (root / name).read_text() != content:
            raise ValueError(f"source differs from the reviewed Week {week} patch: {name}")
    changed = subprocess.check_output(
        ["git", "-C", str(root), "diff", "--name-only", "HEAD"], text=True
    ).splitlines()
    if set(changed) - expected.keys():
        raise ValueError("vLLM has tracked source changes outside the reviewed trace patch")
    import vllm

    if Path(inspect.getfile(vllm)).resolve().parent != root / "vllm":
        raise ValueError(
            "installed vLLM imports a different tree; use the patched editable checkout"
        )
    return {
        "commit": VLLM_COMMIT,
        "package_path": inspect.getfile(vllm),
        "package_version": vllm.__version__,
        "patch_week": week,
    }


def selected_command(config, base, *, execution):
    path = Path(config["operating_point"])
    if path.is_file():
        selection = read_json(path)
        argv = selection.get("operating_point") or selection.get("fallback")
        if argv:
            return argv
    if execution:
        raise ValueError("Week 7 needs a verified Week 6 operating point or fallback")
    return serve_command(base)


def override_command(argv, overrides):
    argv = list(argv)
    allowed = {"max_num_batched_tokens", "enable_chunked_prefill", "num_gpu_blocks_override"}
    if not overrides.keys() <= allowed:
        raise ValueError("scenario changes parameters outside the controlled dimensions")
    for key, value in overrides.items():
        flag = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            argv = [a for a in argv if a not in {flag, "--no-" + flag[2:]}]
            argv.append(flag if value else "--no-" + flag[2:])
        elif flag in argv:
            argv[argv.index(flag) + 1] = str(value)
        else:
            argv += [flag, str(value)]
    return argv


def lifecycle_requests(url, model, timeout, abort_tokens):
    records = []
    for mode in ("nonstream", "stream", "abort"):
        rid = mode + "-" + uuid.uuid4().hex
        payload = {
            "model": model,
            "prompt": "Explain how request queues work.",
            "request_id": rid,
            "temperature": 0,
            "seed": 42,
            "ignore_eos": True,
            "max_tokens": abort_tokens if mode == "abort" else 4,
            "stream": mode != "nonstream",
        }
        record = {"request_id": rid, "mode": mode, "started_ns": time.monotonic_ns()}
        if mode == "nonstream":
            response = http_json(url + "/v1/completions", payload, timeout=timeout)
            if response.get("usage", {}).get("completion_tokens") != 4:
                raise ValueError("nonstream token count mismatch")
            record["usage"] = response["usage"]
        else:
            payload["stream_options"] = {"include_usage": True}
            stream = iter_openai_events(
                stream_sse(url + "/v1/completions", payload, timeout=timeout, max_duration=timeout)
            )
            chunks, done, usage = 0, False, None
            try:
                for event in stream:
                    if event.content:
                        chunks += 1
                        record.setdefault("first_chunk_ns", time.monotonic_ns())
                        if mode == "abort":
                            break
                    done |= event.done
                    usage = event.usage or usage
            finally:
                # Closing the generator closes the HTTP response/socket immediately.
                stream.close()
            if chunks == 0 or (
                mode == "stream" and (not done or not usage or usage.get("completion_tokens") != 4)
            ):
                raise ValueError("stream did not satisfy the completion contract")
            record.update(content_chunks=chunks, done=done)
        record["ended_ns"] = time.monotonic_ns()
        records.append(record)
    try:
        http_json(
            url + "/v1/completions",
            {"model": "nonexistent-study-model", "prompt": "test"},
            timeout=timeout,
        )
    except OpenAIHTTPError as error:
        if error.status not in {400, 404, 422}:
            raise
        records.append({"mode": "validation_failure", "http_status": error.status})
    else:
        raise ValueError("invalid model request unexpectedly succeeded")
    return records


def scenario_requests(url, base, tokenizer, scenario, timeout):
    ids = tokenizer.encode("A deterministic scheduler study prompt. ", add_special_tokens=False)
    jobs = []
    for row in scenario["requests"]:
        n, output = row["prompt_tokens"], row["output_tokens"]
        if n <= 0 or output < 2 or n + output > base["server"]["max_model_len"]:
            raise ValueError("scenario request shape violates model length or TPOT contract")
        jobs.append(
            {
                "request_id": row["request_id"],
                "offset": row["arrival_ms"] / 1000,
                "prompt_ids": (ids * (n // len(ids) + 1))[:n],
                "output_tokens": output,
                "workload": row["request_id"],
            }
        )
    wall, mono = time.time(), time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = []
        for job in jobs:
            time.sleep(max(0, mono + job["offset"] - time.monotonic()))
            futures.append(
                (
                    job,
                    pool.submit(
                        request_record,
                        url,
                        base["model"]["served_model_name"],
                        job,
                        wall,
                        mono,
                        timeout,
                        42,
                    ),
                )
            )
        records = []
        for job, future in futures:
            record = future.result()
            record["scheduled_mono_ns"] = int((mono + job["offset"]) * 1e9)
            records.append(record)
    return records


def run_week07(config, base, argv):
    root = Path(config["output_root"]) / "traces" / str(uuid.uuid4())
    root.mkdir(parents=True, exist_ok=False)
    source = verify_source(config["source_checkout"], 7)
    write_json(root / "source.json", source)
    with managed_server(
        base,
        argv,
        root,
        env={
            "VLLM_STUDY_TRACE_DIR": str(root.resolve()),
            "VLLM_STUDY_TRACE_LIMIT": str(config["trace_event_limit"]),
        },
    ) as (url, _):
        records = lifecycle_requests(
            url,
            base["model"]["served_model_name"],
            config["timeout_seconds"],
            config["abort_max_tokens"],
        )
        write_json(root / "client.json", records)
        deadline = time.monotonic() + config["timeout_seconds"]
        while True:
            events = load_events(root.glob("trace-*.jsonl"))
            try:
                paths = [
                    (r["mode"], validate_path(events, r["request_id"], r["mode"]))
                    for r in records
                    if r["mode"] != "validation_failure"
                ]
                break
            except ValueError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        for mode, events in paths:
            write_json(root / mode / "events.json", events)
            write_text(root / mode / "sequence.mmd", sequence_diagram(events))
        write_text(root / "metrics-after.txt", fetch_text(url + "/metrics"))
    print(f"Request traces and assertions saved in {root}")


def run_week08(config, source_config, base, argv, scenario_name=None):
    source = verify_source(source_config["source_checkout"], 8)
    tokenizer = load_tokenizer(base)
    selected = (
        config["scenarios"].items()
        if scenario_name is None
        else [(scenario_name, config["scenarios"][scenario_name])]
    )
    for name, scenario in selected:
        root = Path(config["output_root"]) / "traces" / name / str(uuid.uuid4())
        root.mkdir(parents=True, exist_ok=False)
        write_json(root / "source.json", source)
        write_json(root / "scenario.json", scenario)
        command = override_command(argv, scenario["overrides"])
        with managed_server(
            base,
            command,
            root,
            env={
                "VLLM_STUDY_TRACE_DIR": str(root.resolve()),
                "VLLM_STUDY_TRACE_LIMIT": str(config["trace_event_limit"]),
            },
        ) as (url, _):
            write_text(root / "metrics-before.txt", fetch_text(url + "/metrics"))
            records = scenario_requests(url, base, tokenizer, scenario, config["timeout_seconds"])
            write_json(root / "client.json", records)
            deadline = time.monotonic() + config["timeout_seconds"]
            while True:
                try:
                    result = parse_events(load_events(root.glob("trace-*.jsonl")))
                    break
                except ValueError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.2)
            result["scenario"] = name
            result["client_success"] = all(r["status"] == "success" for r in records)
            result["pressure_observed"] = bool(
                result["allocation_failures"] or result["preemptions"]
            )
            if name == "kv_pressure" and not result["pressure_observed"]:
                result["not_observed_reason"] = "configured workload did not trigger KV pressure"
            if name == "token_pressure" and not result["chunked_requests"]:
                result["not_observed_reason"] = "no partial prefill observed"
            write_tables(result, root / "analysis")
            write_text(root / "metrics-after.txt", fetch_text(url + "/metrics"))
        draw(result, root / "figures", records)
        print(f"{name}: {root}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--scenario")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--plan", action="store_true")
    modes.add_argument("--run", action="store_true")
    args = parser.parse_args()
    config = load_study(args.config)
    source_config = config if config["week"] == 7 else load_study(config["week07_config"])
    base = load_yaml(source_config["baseline_config"])
    argv = selected_command(source_config, base, execution=args.run)
    if args.plan:
        print(
            json.dumps(
                {
                    "week": config["week"],
                    "source": source_config["source_checkout"],
                    "commit": VLLM_COMMIT,
                    "server_argv": argv,
                    "operating_point_required": source_config["operating_point"],
                    "scenarios": config.get("scenarios", ["nonstream", "stream", "abort"]),
                },
                indent=2,
            )
        )
    elif config["week"] == 7:
        run_week07(config, base, argv)
    else:
        run_week08(config, source_config, base, argv, args.scenario)


if __name__ == "__main__":
    main()
