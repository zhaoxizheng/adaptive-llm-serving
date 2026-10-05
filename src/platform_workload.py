"""Paired synthetic routing/arrival traces, without persisting prompts or tokens."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import random
import threading
import time

from src.experiment_client import request
from src.serving_experiment import summarize


def matrix(cfg):
    """Interleave treatments, preserve workload/arrival seeds within each repeat."""
    cells = []
    names = [name for name, case in cfg["cases"].items() if not case.get("fault")]
    for repeat in range(cfg["repeats"]):
        order = names[repeat % len(names) :] + names[: repeat % len(names)]
        for workload in cfg["workloads"]:
            for rate in cfg["rates"]:
                for cache in cfg["cache_states"]:
                    for name in order:
                        cells.append(
                            dict(
                                case=name,
                                workload=workload,
                                rate=rate,
                                cache=cache,
                                repeat=repeat,
                                seed=cfg["seed"] + repeat,
                            )
                        )
    return cells


def make_jobs(cfg, tokenizer, workload, rate, repeat, *, warmup=False):
    vocab = sorted(set(tokenizer.get_vocab().values()) - set(tokenizer.all_special_ids))
    n = cfg["warmup_requests"] if warmup else cfg["requests"]
    families = cfg["prefix_families"]
    if len(vocab) < n + families + 8:
        raise ValueError("vocabulary too small for distinct synthetic families")
    if rate <= 0:
        raise ValueError("freeze a positive measured offered rate")
    arrivals = random.Random(cfg["seed"] + repeat)
    family_rng = random.Random(cfg["seed"] + repeat + 10000)
    offset, jobs = 0.0, []
    for i in range(n):
        kind = (
            ("long" if i % 4 == 0 else "short")
            if workload in {"mixed", "burst", "ramp", "short_burst", "drain"}
            else "shared"
        )
        if workload in {"short", "long"}:
            kind = workload
        if warmup:
            kind = "short"
        if workload == "drain" and i == n - 1:
            kind = "long"
        shape = cfg["shapes"][kind]
        family = (
            0
            if workload == "hot_prefix" and family_rng.random() < 0.8
            else i % families
        )
        shared = kind == "shared" and workload != "low_sharing" and not warmup
        # Dedicated leading IDs prevent accidental shared blocks in low-sharing controls.
        leading = vocab[family] if shared else vocab[families + i]
        if warmup:
            leading = vocab[-1]
        prefix_rng = random.Random(cfg["seed"] + family)
        suffix_rng = random.Random(cfg["seed"] + repeat * 100000 + i)
        length = cfg["shared_tokens"] if shared else 1
        ids = [leading] + [prefix_rng.choice(vocab) for _ in range(length - 1)]
        ids += [
            suffix_rng.choice(vocab) for _ in range(shape["prompt_tokens"] - length)
        ]
        jobs.append(
            dict(
                request_id=f"r{i:06d}",
                workload=kind,
                prefix_family=family,
                prompt_ids=ids,
                output_tokens=shape["output_tokens"],
                offset=offset,
            )
        )
        multiplier = 1
        if workload in {"burst", "short_burst", "drain"}:
            width = (
                (n // 3, 2 * n // 3)
                if workload != "short_burst"
                else (n // 2, n // 2 + max(1, n // 20))
            )
            multiplier = 4 if width[0] <= i < width[1] else 1
        elif workload == "ramp":
            multiplier = 1 + 3 * max(0, 1 - abs(2 * i / max(1, n - 1) - 1))
        offset += arrivals.expovariate(rate * multiplier)
    if jobs[-1]["offset"] + cfg["timeout_seconds"] > cfg["max_run_seconds"]:
        raise ValueError(
            "arrival trace exceeds bounded cell duration; lower requests or raise rate"
        )
    return jobs


def trace_metadata(jobs):
    wire = json.dumps(jobs, sort_keys=True, separators=(",", ":")).encode()
    return dict(
        sha256=hashlib.sha256(wire).hexdigest(),
        requests=len(jobs),
        last_arrival_seconds=jobs[-1]["offset"],
        contains_synthetic_tokens=True,
    )


def safe_row(row, job):
    # Explicit allowlist: the reused SSE parser keeps text in memory for validation only.
    fields = {
        "request_id",
        "workload",
        "prompt_tokens",
        "output_tokens",
        "scheduled_at",
        "started_at",
        "completed_at",
        "first_content_at",
        "arrival_lag_ms",
        "status",
        "http_status",
        "error_type",
        "ttft_ms",
        "tpot_ms",
        "usage",
        "finish_reason",
    }
    return {
        **{k: v for k, v in row.items() if k in fields},
        "prefix_family": job["prefix_family"],
    }


def run_requests(cfg, base, jobs, path, *, cancel=False):
    wall, mono = time.time(), time.monotonic()
    slots, lock = threading.BoundedSemaphore(cfg["max_inflight"]), threading.Lock()
    rows = []
    with (
        path.open("x") as handle,
        path.with_name(path.stem + "-arrivals.jsonl").open("x") as arrivals,
    ):

        def save(row, job):
            row = safe_row(row, job)
            with lock:
                rows.append(row)
                handle.write(json.dumps(row, allow_nan=False) + "\n")
                handle.flush()

        def execute(job):
            try:
                row = request(
                    cfg["endpoint"],
                    base["model"]["served_model_name"],
                    job,
                    wall,
                    mono,
                    cfg["timeout_seconds"],
                    cfg["seed"],
                    headers={"Host": cfg["hostname"]},
                    cancel=cancel,
                )
                save(row, job)
            finally:
                slots.release()

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=cfg["max_inflight"]
        ) as executor:
            futures = []
            for job in jobs:
                time.sleep(max(0, mono + job["offset"] - time.monotonic()))
                arrivals.write(
                    json.dumps(
                        dict(
                            request_id=job["request_id"],
                            scheduled_at=wall + job["offset"],
                            offered_at=time.time(),
                        )
                    )
                    + "\n"
                )
                arrivals.flush()
                if slots.acquire(blocking=False):
                    futures.append(executor.submit(execute, job))
                else:
                    save(
                        dict(
                            request_id=job["request_id"],
                            workload=job["workload"],
                            status="client_overload",
                            output_tokens=job["output_tokens"],
                            scheduled_at=wall + job["offset"],
                            completed_at=time.time(),
                            ttft_ms=None,
                            tpot_ms=None,
                            arrival_lag_ms=None,
                        ),
                        job,
                    )
            for future in futures:
                future.result()
    elapsed = time.monotonic() - mono
    return rows, summarize(rows, base["slo"], elapsed)
