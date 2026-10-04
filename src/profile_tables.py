"""CPU-only table and interval utilities shared by both profiler analyzers."""

import csv
import json
import math
from pathlib import Path


def write_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(
            {k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in row.items()}
            for row in rows
        )


def union_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if not math.isfinite(start) or not math.isfinite(end) or end < start:
            raise ValueError("invalid timeline interval")
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


def paired_metrics(baseline, captured):
    def summarize(rows):
        if not rows or any(r["status"] != "success" for r in rows):
            raise ValueError("failed or empty client run cannot form a baseline pair")
        span = max(r["completed_at"] for r in rows) - min(r["scheduled_at"] for r in rows)
        if span <= 0:
            raise ValueError("invalid workload wall interval")
        return dict(
            wall_seconds=span,
            output_tokens_per_second=sum(r["output_tokens"] for r in rows) / span,
            mean_ttft_ms=sum(r["ttft_ms"] for r in rows) / len(rows),
            mean_tpot_ms=(
                sum(r["tpot_ms"] for r in rows if r["tpot_ms"] is not None)
                / sum(r["tpot_ms"] is not None for r in rows)
                if any(r["tpot_ms"] is not None for r in rows)
                else None
            ),
        )

    def identity(rows):
        return [(r["request_id"], r["prompt_sha256"], r["output_tokens"]) for r in rows]

    if identity(baseline) != identity(captured):
        raise ValueError("baseline/profile workload differs")
    before, after = summarize(baseline), summarize(captured)
    return dict(
        baseline=before,
        captured=after,
        wall_overhead_ratio=after["wall_seconds"] / before["wall_seconds"] - 1,
        interpretation="short paired workload wall time; includes instrumentation/export stalls",
    )
