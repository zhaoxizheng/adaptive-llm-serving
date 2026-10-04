"""Copied into the pinned vLLM tree as vllm/_study_trace.py by the patch builder."""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()
_COUNT = 0
_FIELDS = {
    "prompt_tokens",
    "max_tokens",
    "finished",
    "finish_reason",
    "step",
    "state",
    "running_before",
    "waiting_before",
    "running_after",
    "waiting_after",
    "token_budget_initial",
    "token_budget_remaining",
    "max_num_seqs",
    "scheduled",
    "preempted",
    "reason",
    "kv_usage",
    "request_ids",
    "num_output_tokens",
}


def enabled():
    return bool(os.environ.get("VLLM_STUDY_TRACE_DIR"))


def emit(component, event, request_id=None, **fields):
    global _COUNT
    if not enabled():
        return
    if fields.keys() - _FIELDS:
        raise ValueError("non-allowlisted study trace fields")
    with _LOCK:
        limit = int(os.environ.get("VLLM_STUDY_TRACE_LIMIT", "100000"))
        if _COUNT > limit:
            return
        if _COUNT == limit:
            event, request_id, fields = "trace_truncated", None, {}
        record = {
            "schema_version": 1,
            "ts_ns": time.monotonic_ns(),
            "wall_time_utc": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "thread": threading.get_ident(),
            "component": component,
            "event": event,
            "request_id": request_id,
            **fields,
        }
        directory = Path(os.environ["VLLM_STUDY_TRACE_DIR"])
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / f"trace-{os.getpid()}.jsonl").open("a") as handle:
            handle.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
        _COUNT += 1
