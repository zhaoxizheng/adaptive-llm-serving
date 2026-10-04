"""Join scheduler and runner evidence by the step transported in SchedulerOutput."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from src.common import write_json
from src.parse_scheduler_trace import load_events
from src.profile_tables import write_csv


def parse_events(events):
    schedules, shapes, outputs = {}, {}, set()
    ranges, graphs = defaultdict(list), defaultdict(set)
    scheduler_pids, runner_pids = set(), set()
    for e in events:
        if e["component"] != "execution":
            continue
        kind, step = e["event"], e["step"]
        if step < 0:  # Initialization/capture is separate from request execution.
            continue
        if kind == "schedule":
            if step in schedules:
                raise ValueError("duplicate scheduler step")
            schedules[step] = e["scheduled"]
            scheduler_pids.add(e["pid"])
        elif kind == "shape":
            if step in shapes:
                raise ValueError("duplicate shape step")
            shapes[step] = dict(e)
            runner_pids.add(e["pid"])
        elif kind == "boundary":
            if e["cpu_duration_us"] < 0 or e["start_ns"] > e["ts_ns"]:
                raise ValueError("invalid CPU boundary interval")
            ranges[step].append(e)
        elif kind in {"graph_capture", "graph_replay"}:
            graphs[step].add(kind)
        elif kind == "output":
            if step in outputs:
                raise ValueError("duplicate runner output")
            outputs.add(step)
        elif kind != "capture_step":
            raise ValueError(f"unknown execution event {kind}")
    if not shapes or len(scheduler_pids) != 1 or len(runner_pids) != 1:
        raise ValueError("need one scheduler and one GPU runner with shape evidence")
    if sorted(schedules) != list(range(len(schedules))):
        raise ValueError("incomplete scheduler step stream")
    if any(rows and step not in shapes for step, rows in schedules.items()):
        raise ValueError("scheduled work has no model shape")
    for step, row in shapes.items():
        if step not in schedules or step not in outputs:
            raise ValueError("shape missing scheduler or terminal output")
        scheduled = schedules[step]
        if len({r["request_id"] for r in scheduled}) != len(scheduled):
            raise ValueError("duplicate scheduled request")
        total = sum(r["tokens"] for r in scheduled)
        prefill = sum(
            min(r["tokens"], max(0, r["prompt_tokens"] - r["computed_tokens"])) for r in scheduled
        )
        if (
            total <= 0
            or row["scheduled_tokens"] != total
            or row["prefill_tokens"] != prefill
            or row["decode_tokens"] != total - prefill
            or row["kv_slot_count"] != total
            or row["request_count"] != len(scheduled)
        ):
            raise ValueError("scheduler/composition/slot mismatch")
        padded = row["padded_tokens"]
        if (
            padded < total
            or row["input_ids_shape"] != [padded]
            or row["positions_shape"] != [padded]
        ):
            raise ValueError("invalid logical/padded input shape")
        observed = graphs[step]
        if row["dispatch_mode"] == "NONE" and observed:
            raise ValueError("graph event contradicts eager dispatch")
        row["execution_mode"] = (
            "graph_capture"
            if "graph_capture" in observed
            else (
                "graph_replay"
                if "graph_replay" in observed
                else "eager" if row["dispatch_mode"] == "NONE" else "unproven_graph"
            )
        )
        row["cpu_prepare_us"] = sum(
            r["cpu_duration_us"]
            for r in ranges[step]
            if r["phase"] in {"prepare", "preprocess", "update_batch"}
        )
        row["composition"] = (
            "mixed" if prefill and total - prefill else "prefill" if prefill else "decode"
        )
        row["gpu_execute_us"] = None
    return dict(
        steps=list(shapes.values()),
        boundaries=[r for rs in ranges.values() for r in rs],
        clock="host monotonic_ns; CPU ranges are not GPU kernel durations",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = parse_events(load_events(Path(args.trace_dir).glob("execution-*.jsonl")))
    root = Path(args.output)
    write_json(root / "analysis.json", result)
    write_csv(root / "shapes.csv", result["steps"])


if __name__ == "__main__":
    main()
