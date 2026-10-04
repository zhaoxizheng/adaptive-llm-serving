"""Assert API/IPC/output/cancellation boundaries and emit Mermaid trace diagrams."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.common import write_json, write_text
from src.parse_scheduler_trace import load_events


def validate_path(events, request_id, mode):
    parent = "cmpl-" + request_id
    internal = parent + "-0"
    selected = [e for e in events if e.get("request_id") in {parent, internal}]
    required = {
        "request_received",
        "ipc_submit",
        "core_received",
        "request_queued",
        "request_freed",
    }
    required |= (
        {"abort_sent", "abort_received"} if mode == "abort" else {"core_output", "async_output"}
    )
    required |= {"response_completed"} if mode == "nonstream" else {"http_chunk"}
    seen = {e["event"] for e in selected}
    missing = sorted(required - seen)
    if missing:
        raise ValueError(f"{mode} missing events: {missing}")
    first = {name: next(e for e in selected if e["event"] == name) for name in required}
    for before, after in (
        ("request_received", "ipc_submit"),
        ("ipc_submit", "core_received"),
        ("core_received", "request_queued"),
    ):
        if first[before]["ts_ns"] > first[after]["ts_ns"]:
            raise ValueError("request boundary order violated")
    if first["ipc_submit"]["pid"] == first["core_received"]["pid"]:
        raise ValueError("trace does not prove the API/Engine Core process boundary")
    if mode == "abort":
        freed = first["request_freed"]
        if freed.get("finish_reason") != "FINISHED_ABORTED":
            raise ValueError("abort raced normal completion; cancellation is not proven")
        if not first["abort_sent"]["ts_ns"] <= first["abort_received"]["ts_ns"] <= freed["ts_ns"]:
            raise ValueError("cancellation propagation order violated")
    return selected


def sequence_diagram(events):
    lines = [
        "sequenceDiagram",
        "    participant api",
        "    participant async_llm",
        "    participant engine_core",
        "    participant scheduler",
    ]
    origin = events[0]["ts_ns"]
    for event in events:
        component = event["component"]
        if component not in {"api", "async_llm", "engine_core", "scheduler"}:
            continue
        # Event names are fixed instrumentation symbols, never generated content.
        lines.append(
            f"    Note over {component}: {event['event']} "
            f"pid={event['pid']} +{(event['ts_ns'] - origin) / 1e6:.3f}ms"
        )
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--mode", choices=["nonstream", "stream", "abort"], required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    events = load_events(sorted(Path(args.trace_dir).glob("trace-*.jsonl")))
    selected = validate_path(events, args.request_id, args.mode)
    output = Path(args.output)
    write_json(output / "events.json", selected)
    write_text(output / "sequence.mmd", sequence_diagram(selected))


if __name__ == "__main__":
    main()
