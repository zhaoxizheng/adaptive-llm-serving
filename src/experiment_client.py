"""Bounded open-loop load, durable per-request evidence and cancellation smoke."""

from __future__ import annotations

import concurrent.futures
import json
import threading
import time

from src.openai_stream import OpenAIHTTPError, iter_openai_events, stream_sse
from src.study_contract import fingerprint


def request(url, model, job, wall, mono, timeout, seed, *, headers=None, cancel=False):
    due = mono + job["offset"]
    start = time.monotonic()
    row = dict(request_id=job["request_id"], workload=job["workload"],
               prompt_sha256=fingerprint(job["prompt_ids"]), prompt_tokens=len(job["prompt_ids"]),
               output_tokens=job["output_tokens"], scheduled_at=wall + job["offset"],
               started_at=wall + start - mono, arrival_lag_ms=(start - due) * 1000,
               status="invalid_response")
    payload = dict(model=model, prompt=job["prompt_ids"], max_tokens=job["output_tokens"],
                   request_id=job["request_id"], temperature=0, seed=seed, ignore_eos=True,
                   stream_options={"include_usage": True})
    first = last = None
    done, usage, finish = False, None, None
    output = []
    events = iter_openai_events(stream_sse(
        url, payload, headers={**(headers or {}), "X-Request-ID": job["request_id"]},
        timeout=timeout, max_duration=timeout))
    try:
        for event in events:
            if event.content:
                output.append(event.content)
                last = time.monotonic()
                if first is None:
                    first = last
                if cancel:
                    row["status"] = "client_cancelled"
                    break
            usage = event.usage or usage
            finish = event.finish_reason or finish
            done |= event.done
        if (done and first is not None and finish == "length" and usage
                and usage.get("prompt_tokens") == len(job["prompt_ids"])
                and usage.get("completion_tokens") == job["output_tokens"]):
            row["status"] = "success"
    except TimeoutError:
        row["status"] = "timeout"
    except OpenAIHTTPError as error:
        row.update(status="http_error", http_status=error.status)
    except Exception as error:
        row.update(status="stream_error", error_type=type(error).__name__)
    finally:
        events.close()
    row.update(completed_at=wall + time.monotonic() - mono,
               first_content_at=None if first is None else wall + first - mono,
               ttft_ms=None if first is None else (first - due) * 1000,
               tpot_ms=None if first is None or job["output_tokens"] == 1 else (last - first) * 1000 / (job["output_tokens"] - 1),
               usage=usage, finish_reason=finish,
               output_sha256=fingerprint("".join(output)), output_excerpt="".join(output)[:2048])
    return row


def run_load(url, model, jobs, cfg, journal_path, *, headers=None):
    """Do not queue behind an unbounded executor: client overload stays in the denominator."""
    records, lock = [], threading.Lock()
    slots = threading.BoundedSemaphore(cfg["max_inflight"])
    wall, mono = time.time(), time.monotonic()
    with open(journal_path, "x") as journal:
        def persist(row):
            with lock:
                records.append(row)
                journal.write(json.dumps(row, allow_nan=False) + "\n")
                journal.flush()
        def execute(job):
            try:
                persist(request(url, model, job, wall, mono, cfg["timeout_seconds"], cfg["seed"], headers=headers))
            finally:
                slots.release()
        with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["max_inflight"]) as pool:
            futures = []
            for job in jobs:
                time.sleep(max(0, mono + job["offset"] - time.monotonic()))
                if slots.acquire(blocking=False):
                    futures.append(pool.submit(execute, job))
                else:
                    persist(dict(request_id=job["request_id"], workload=job["workload"],
                                 status="client_overload", output_tokens=job["output_tokens"],
                                 prompt_sha256=fingerprint(job["prompt_ids"]),
                                 scheduled_at=wall + job["offset"], completed_at=time.time(),
                                 ttft_ms=None, tpot_ms=None, arrival_lag_ms=None))
            for future in futures:
                future.result()
    return sorted(records, key=lambda r: r["request_id"]), time.monotonic() - mono
