"""Summarize exported PyTorch operators and raw Chrome trace without importing torch."""

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from pathlib import Path

from src.common import read_json, write_json
from src.profile_tables import union_intervals, write_csv


def summarize(trace, operators):
    events = trace.get("traceEvents", [])
    kernels, apis, memory, shapes = [], [], [], []
    totals = defaultdict(lambda: dict(count=0, total_us=0.0))
    for event in events:
        category = event.get("cat", "")
        if event.get("name") == "[memory]":
            args = event.get("args", {})
            memory.append(
                {
                    "ts_us": event.get("ts"),
                    **{
                        k: args.get(k)
                        for k in (
                            "Bytes",
                            "Total Allocated",
                            "Total Reserved",
                            "Device Type",
                            "Device Id",
                        )
                    },
                }
            )
        if event.get("ph") != "X":
            continue
        ts, duration = event.get("ts"), event.get("dur")
        if (
            not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (ts, duration))
            or duration < 0
        ):
            raise ValueError("invalid profiler duration")
        row = dict(
            name=event.get("name"),
            ts_us=ts,
            duration_us=duration,
            pid=event.get("pid"),
            tid=event.get("tid"),
        )
        if category == "kernel":
            kernels.append(row)
            totals[row["name"]]["count"] += 1
            totals[row["name"]]["total_us"] += duration
        elif category == "cuda_runtime":
            apis.append(row)
        if "Input Dims" in event.get("args", {}):
            shapes.append({**row, "input_shapes": event["args"]["Input Dims"]})
    if not kernels or not operators:
        raise ValueError("capture lacks CUDA kernels or operator summaries")
    for row in operators:
        for key in ("self_cpu_us", "cpu_total_us", "self_device_us", "device_total_us"):
            if not math.isfinite(row[key]) or row[key] < 0:
                raise ValueError("invalid operator timing")
    return dict(
        cpu_operators=sorted(operators, key=lambda r: r["self_cpu_us"], reverse=True),
        cuda_operators=sorted(operators, key=lambda r: r["self_device_us"], reverse=True),
        kernels=[dict(name=name, **values) for name, values in totals.items()],
        cuda_api=apis,
        synchronization=[r for r in apis if "Synchronize" in r["name"]],
        memory=memory,
        shapes=shapes,
        kernel_busy_us=sum(
            b - a
            for a, b in union_intervals(
                (r["ts_us"], r["ts_us"] + r["duration_us"]) for r in kernels
            )
        ),
        units="Chrome trace microseconds; operators are aggregated, not request latency",
    )


def write_summary(trace_path, operators_path, output):
    result = summarize(read_json(trace_path), read_json(operators_path))
    root = Path(output)
    for name in (
        "cpu_operators",
        "cuda_operators",
        "kernels",
        "cuda_api",
        "synchronization",
        "memory",
        "shapes",
    ):
        write_csv(root / f"{name}.csv", result[name])
    write_json(root / "summary.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--operators", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    write_summary(args.trace, args.operators, args.output)


if __name__ == "__main__":
    main()
