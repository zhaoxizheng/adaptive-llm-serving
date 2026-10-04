from __future__ import annotations

import argparse
import csv
import io
from collections import defaultdict
from hashlib import sha256
from pathlib import Path
from typing import Mapping, Sequence

import matplotlib.pyplot as plt
import pandas as pd

from src.common import load_yaml, read_json, write_json, write_text
from src.metrics import percentile
from src.week03_contract import (
    BATCH_FIELDS,
    EVENT_FIELDS,
    SCHEMA_VERSION,
    validate_case_artifacts,
    validate_run_metadata,
)

FIGURE_NAMES = (
    "offered-load-vs-throughput.png",
    "offered-load-vs-p95-p99-ttft.png",
    "batch-window-vs-fill-and-queue-delay.png",
    "queue-depth-over-time-overload.png",
)

CASE_IDENTITY_FIELDS = (
    "run_id",
    "metadata_fingerprint",
    "config_fingerprint",
    "calibration_id",
    "case_id",
    "trace_id",
)
CASE_DIMENSION_FIELDS = (
    "profile",
    "policy",
    "offered_load_ratio",
    "arrival_rate_rps",
    "repeat",
    "max_batch_size",
    "delay_ms",
)
SUMMARY_FIELDS = [
    *CASE_IDENTITY_FIELDS,
    *CASE_DIMENSION_FIELDS,
    "attempted",
    "completed",
    "rejected",
    "failed",
    "measurement_duration_seconds",
    "achieved_throughput_rps",
    "drain_inclusive_throughput_rps",
    "completion_rate",
    "rejection_rate",
    "failure_rate",
    "p50_ttft_ms",
    "p95_ttft_ms",
    "p99_ttft_ms",
    "p50_e2e_ms",
    "p95_e2e_ms",
    "p99_e2e_ms",
    "p50_queue_delay_ms",
    "p95_queue_delay_ms",
    "p99_queue_delay_ms",
    "p50_arrival_lag_ms",
    "p99_arrival_lag_ms",
    "mean_batch_fill_ratio",
    "max_queue_depth",
]


def _read_csv_and_sha256(
    path: str | Path, fields: Sequence[str]
) -> tuple[list[dict[str, str]], str]:
    source = Path(path)
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError(f"missing or empty Week 3 evidence: {source}")
    payload = source.read_bytes()
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"Week 3 evidence is not UTF-8: {source}") from error
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != list(fields):
        raise ValueError(
            f"Week 3 CSV header mismatch: expected {list(fields)}, got {reader.fieldnames}"
        )
    rows = list(reader)
    if not rows:
        raise ValueError(f"Week 3 evidence has no rows: {source}")
    return rows, sha256(payload).hexdigest()


def _nearest(values: Sequence[float], fraction: float) -> float | None:
    return percentile(list(values), fraction) if values else None


def _case_values(
    event_rows: Sequence[Mapping[str, object]],
    batch_rows: Sequence[Mapping[str, object]],
) -> dict[str, str]:
    rows = [*event_rows, *batch_rows]
    values: dict[str, str] = {}
    for field in ("schema_version", *CASE_IDENTITY_FIELDS, *CASE_DIMENSION_FIELDS):
        missing = [index for index, row in enumerate(rows, start=1) if field not in row]
        if missing:
            raise ValueError(f"Week 3 case row is missing {field}")
        distinct = {str(row[field]) for row in rows}
        if len(distinct) != 1:
            raise ValueError(f"Week 3 case has mixed {field}: {sorted(distinct)}")
        value = distinct.pop()
        if field in CASE_IDENTITY_FIELDS and not value:
            raise ValueError(f"Week 3 case has blank {field}")
        values[field] = value
    return values


def summarize_case(
    event_rows: Sequence[Mapping[str, object]],
    batch_rows: Sequence[Mapping[str, object]],
    *,
    metadata: Mapping[str, object] | None = None,
    measurement_duration_seconds: float | None = None,
) -> dict[str, object]:
    if not event_rows:
        raise ValueError("cannot summarize an empty Week 3 case")
    values = _case_values(event_rows, batch_rows)
    validate_case_artifacts(
        event_rows,
        batch_rows,
        expected_run_id=values["run_id"],
        expected_case_id=values["case_id"],
        expected_metadata=metadata,
        expected_trace_id=values["trace_id"],
    )
    measurement = [
        row for row in event_rows if str(row["measurement"]).lower() in {"true", "1"}
    ]
    if not measurement:
        raise ValueError("Week 3 case has no measurement-window requests")
    completed = [row for row in measurement if row["status"] == "completed"]
    rejected = [row for row in measurement if row["status"] == "rejected"]
    failed = [row for row in measurement if row["status"] == "failed"]
    terminal = [int(str(row["terminal_ns"])) for row in measurement]
    if measurement_duration_seconds is None:
        if metadata is None:
            raise ValueError(
                "measurement_duration_seconds or run metadata is required for throughput"
            )
        scientific = metadata.get("scientific_config")
        if not isinstance(scientific, Mapping):
            raise ValueError("Week 3 metadata lacks scientific_config")
        workload = scientific.get("workload")
        matrix = scientific.get("matrix")
        if not isinstance(workload, Mapping) or not isinstance(matrix, Mapping):
            raise ValueError("Week 3 metadata lacks workload/matrix duration evidence")
        profile_config = matrix.get(values["profile"])
        if not isinstance(profile_config, Mapping):
            raise ValueError("Week 3 metadata lacks the case profile config")
        duration = float(
            profile_config.get("duration_seconds", workload["duration_seconds"])
        )
        warmup = float(
            profile_config.get("warmup_seconds", workload["warmup_seconds"])
        )
        measurement_duration_seconds = duration - warmup
    if measurement_duration_seconds <= 0:
        raise ValueError("measurement duration must be positive")
    scientific_warmup_seconds = 0.0
    if metadata is not None:
        scientific = metadata.get("scientific_config")
        if isinstance(scientific, Mapping):
            workload = scientific.get("workload")
            matrix = scientific.get("matrix")
            if isinstance(workload, Mapping) and isinstance(matrix, Mapping):
                profile_config = matrix.get(values["profile"])
                if isinstance(profile_config, Mapping):
                    scientific_warmup_seconds = float(
                        profile_config.get("warmup_seconds", workload["warmup_seconds"])
                    )
    measurement_start_ns = round(scientific_warmup_seconds * 1_000_000_000)
    drain_elapsed_ns = max(1, max(terminal) - measurement_start_ns)
    ttft_ms = [int(str(row["ttft_ns"])) / 1_000_000 for row in completed]
    e2e_ms = [int(str(row["e2e_latency_ns"])) / 1_000_000 for row in completed]
    queue_ms = [int(str(row["queueing_delay_ns"])) / 1_000_000 for row in completed]
    arrival_lag_ms = [
        int(str(row["arrival_lag_ns"])) / 1_000_000 for row in measurement
    ]
    case_batches = {str(row["batch_id"]) for row in measurement if row["batch_id"]}
    measurement_batches = [row for row in batch_rows if row["batch_id"] in case_batches]
    fill = [float(str(row["fill_ratio"])) for row in measurement_batches]
    attempted = len(measurement)
    return {
        "run_id": values["run_id"],
        "metadata_fingerprint": values["metadata_fingerprint"],
        "config_fingerprint": values["config_fingerprint"],
        "calibration_id": values["calibration_id"],
        "case_id": values["case_id"],
        "trace_id": values["trace_id"],
        "profile": values["profile"],
        "policy": values["policy"],
        "offered_load_ratio": float(values["offered_load_ratio"]),
        "arrival_rate_rps": float(values["arrival_rate_rps"]),
        "repeat": int(values["repeat"]),
        "max_batch_size": int(values["max_batch_size"]),
        "delay_ms": int(values["delay_ms"]),
        "attempted": attempted,
        "completed": len(completed),
        "rejected": len(rejected),
        "failed": len(failed),
        "measurement_duration_seconds": measurement_duration_seconds,
        "achieved_throughput_rps": len(completed) / measurement_duration_seconds,
        "drain_inclusive_throughput_rps": len(completed)
        / (drain_elapsed_ns / 1_000_000_000),
        "completion_rate": len(completed) / attempted,
        "rejection_rate": len(rejected) / attempted,
        "failure_rate": len(failed) / attempted,
        "p50_ttft_ms": _nearest(ttft_ms, 0.50),
        "p95_ttft_ms": _nearest(ttft_ms, 0.95),
        "p99_ttft_ms": _nearest(ttft_ms, 0.99),
        "p50_e2e_ms": _nearest(e2e_ms, 0.50),
        "p95_e2e_ms": _nearest(e2e_ms, 0.95),
        "p99_e2e_ms": _nearest(e2e_ms, 0.99),
        "p50_queue_delay_ms": _nearest(queue_ms, 0.50),
        "p95_queue_delay_ms": _nearest(queue_ms, 0.95),
        "p99_queue_delay_ms": _nearest(queue_ms, 0.99),
        "p50_arrival_lag_ms": _nearest(arrival_lag_ms, 0.50),
        "p99_arrival_lag_ms": _nearest(arrival_lag_ms, 0.99),
        "mean_batch_fill_ratio": sum(fill) / len(fill) if fill else None,
        "max_queue_depth": max(
            int(str(row["queue_depth_at_admission"])) for row in measurement
        ),
    }


def summarize_results(
    event_rows: Sequence[Mapping[str, object]],
    batch_rows: Sequence[Mapping[str, object]],
    *,
    metadata: Mapping[str, object] | None = None,
    measurement_duration_seconds: float | None = None,
) -> list[dict[str, object]]:
    grouped_events: dict[tuple[str, str], list[Mapping[str, object]]] = defaultdict(
        list
    )
    grouped_batches: dict[tuple[str, str], list[Mapping[str, object]]] = defaultdict(
        list
    )
    for row in event_rows:
        grouped_events[(str(row["run_id"]), str(row["case_id"]))].append(row)
    for row in batch_rows:
        grouped_batches[(str(row["run_id"]), str(row["case_id"]))].append(row)
    extra_batch_cases = set(grouped_batches).difference(grouped_events)
    if extra_batch_cases:
        raise ValueError(
            f"Week 3 batches have no matching event cases: {sorted(extra_batch_cases)}"
        )
    return [
        summarize_case(
            grouped_events[key],
            grouped_batches.get(key, []),
            metadata=metadata,
            measurement_duration_seconds=measurement_duration_seconds,
        )
        for key in sorted(grouped_events)
    ]


def _ordered_summaries(
    summaries: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    keys: set[tuple[str, str]] = set()
    ordered: list[tuple[tuple[str, str], dict[str, object]]] = []
    for row in summaries:
        if set(row) != set(SUMMARY_FIELDS):
            missing = sorted(set(SUMMARY_FIELDS).difference(row))
            extra = sorted(set(row).difference(SUMMARY_FIELDS))
            raise ValueError(
                f"Week 3 summary row schema mismatch; missing={missing}, extra={extra}"
            )
        key = (str(row["run_id"]), str(row["case_id"]))
        if key in keys:
            raise ValueError(f"duplicate Week 3 summary identity: {key}")
        keys.add(key)
        ordered.append((key, {field: row[field] for field in SUMMARY_FIELDS}))
    return [row for _, row in sorted(ordered, key=lambda item: item[0])]


def summary_csv_text(summaries: Sequence[Mapping[str, object]]) -> str:
    """Serialize Week 3 summaries with a stable schema, order, and newline."""

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=SUMMARY_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(_ordered_summaries(summaries))
    return buffer.getvalue()


def analysis_payload(
    summaries: Sequence[Mapping[str, object]],
    *,
    metadata: Mapping[str, object],
    events_sha256: str,
    batches_sha256: str,
    figures: Sequence[str | Path],
) -> dict[str, object]:
    """Build the deterministic raw-evidence-to-analysis manifest."""

    ordered = _ordered_summaries(summaries)
    required_metadata = (
        "run_id",
        "metadata_fingerprint",
        "config_fingerprint",
        "calibration_id",
        "profile",
        "backend",
    )
    missing = [field for field in required_metadata if not metadata.get(field)]
    if missing:
        raise ValueError(f"Week 3 metadata is missing analysis identity: {missing}")
    for row in ordered:
        for field in (
            "run_id",
            "metadata_fingerprint",
            "config_fingerprint",
            "calibration_id",
            "profile",
        ):
            if str(row[field]) != str(metadata[field]):
                raise ValueError(f"Week 3 summary {field} does not match run metadata")
    for digest_name, digest in (
        ("events.csv", events_sha256),
        ("batches.csv", batches_sha256),
    ):
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError(f"invalid SHA-256 for {digest_name}")
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": metadata["run_id"],
        "metadata_fingerprint": metadata["metadata_fingerprint"],
        "config_fingerprint": metadata["config_fingerprint"],
        "calibration_id": metadata["calibration_id"],
        "profile": metadata["profile"],
        "backend": metadata["backend"],
        "input_sha256": {
            "events.csv": events_sha256,
            "batches.csv": batches_sha256,
        },
        "case_count": len(ordered),
        "figures": [str(path) for path in figures],
        "summaries": [dict(row) for row in ordered],
    }


def reconstruct_queue_depth(
    event_rows: Sequence[Mapping[str, object]],
) -> list[tuple[int, int]]:
    """Reconstruct admitted-but-not-dispatched queue depth from raw events."""

    changes: dict[int, int] = defaultdict(int)
    for row in event_rows:
        if row["admitted_ns"] not in ("", None):
            changes[int(str(row["admitted_ns"]))] += 1
        if row["dispatch_ns"] not in ("", None):
            changes[int(str(row["dispatch_ns"]))] -= 1
    depth = 0
    points: list[tuple[int, int]] = []
    for timestamp in sorted(changes):
        depth += changes[timestamp]
        if depth < 0:
            raise ValueError("queue reconstruction became negative")
        points.append((timestamp, depth))
    return points


def _median_summary(summary: pd.DataFrame, dimensions: Sequence[str]) -> pd.DataFrame:
    non_metrics = {*CASE_IDENTITY_FIELDS, "profile", "policy"}
    numeric = [
        column
        for column in summary.columns
        if column not in non_metrics and pd.api.types.is_numeric_dtype(summary[column])
    ]
    return summary.groupby(list(dimensions), as_index=False)[numeric].median()


def _plot_throughput(summary: pd.DataFrame, path: Path) -> None:
    base = _median_summary(summary, ["policy", "offered_load_ratio"])
    _, axis = plt.subplots(figsize=(9, 5))
    for policy, group in base.groupby("policy"):
        ordered = group.sort_values("offered_load_ratio")
        axis.plot(
            ordered["offered_load_ratio"],
            ordered["achieved_throughput_rps"],
            marker="o",
            label=policy,
        )
    requested = (
        base.groupby("offered_load_ratio")["arrival_rate_rps"].median().sort_index()
    )
    axis.plot(
        requested.index,
        requested.values,
        linestyle="--",
        color="black",
        label="offered",
    )
    axis.set(
        xlabel="Offered load / calibrated capacity",
        ylabel="Requests/s",
        title="Offered load vs achieved throughput",
    )
    axis.grid(alpha=0.3)
    axis.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=160)
    plt.close()


def _plot_ttft(summary: pd.DataFrame, path: Path) -> None:
    if not any(
        pd.to_numeric(summary[column], errors="coerce").notna().any()
        for column in ("p95_ttft_ms", "p99_ttft_ms")
    ):
        raise ValueError("Week 3 evidence has no completed TTFT samples to plot")
    base = _median_summary(summary, ["policy", "offered_load_ratio"])
    _, axis = plt.subplots(figsize=(9, 5))
    plotted = False
    for policy, group in base.groupby("policy"):
        ordered = group.dropna(subset=["p95_ttft_ms", "p99_ttft_ms"]).sort_values(
            "offered_load_ratio"
        )
        if ordered.empty:
            continue
        axis.plot(
            ordered["offered_load_ratio"],
            ordered["p95_ttft_ms"],
            marker="o",
            label=f"{policy} P95",
        )
        axis.plot(
            ordered["offered_load_ratio"],
            ordered["p99_ttft_ms"],
            marker="x",
            linestyle="--",
            label=f"{policy} P99",
        )
        plotted = True
    if not plotted:
        plt.close()
        raise ValueError("Week 3 evidence has no completed TTFT samples to plot")
    axis.set(
        xlabel="Offered load / calibrated capacity",
        ylabel="TTFT (ms)",
        title="Tail TTFT under offered load",
    )
    axis.grid(alpha=0.3)
    axis.legend(fontsize=8, ncol=2)
    plt.tight_layout()
    plt.savefig(path, dpi=160)
    plt.close()


def _plot_window(summary: pd.DataFrame, path: Path) -> None:
    sweep = summary[
        (summary["policy"] != "no_batching") & (summary["offered_load_ratio"] == 0.75)
    ]
    if sweep.empty:
        raise ValueError("Week 3 evidence has no 0.75 parameter sweep")
    plottable_fields = [
        column
        for column in ("mean_batch_fill_ratio", "p95_queue_delay_ms")
        if pd.to_numeric(sweep[column], errors="coerce").notna().any()
    ]
    if not plottable_fields:
        raise ValueError("Week 3 parameter sweep has no batch or queue samples to plot")
    base = _median_summary(sweep, ["policy", "max_batch_size", "delay_ms"])
    figure, left = plt.subplots(figsize=(10, 5))
    right = left.twinx()
    plotted = False
    for (policy, size), group in base.groupby(["policy", "max_batch_size"]):
        ordered = group.sort_values("delay_ms")
        label = f"{policy}, batch={size}"
        fill = (
            ordered.dropna(subset=["mean_batch_fill_ratio"])
            if "mean_batch_fill_ratio" in base
            else ordered.iloc[0:0]
        )
        queue = (
            ordered.dropna(subset=["p95_queue_delay_ms"])
            if "p95_queue_delay_ms" in base
            else ordered.iloc[0:0]
        )
        if not fill.empty:
            left.plot(
                fill["delay_ms"],
                fill["mean_batch_fill_ratio"],
                marker="o",
                label=f"fill: {label}",
            )
            plotted = True
        if not queue.empty:
            right.plot(
                queue["delay_ms"],
                queue["p95_queue_delay_ms"],
                marker="x",
                linestyle="--",
                label=f"queue P95: {label}",
            )
            plotted = True
    if not plotted:
        plt.close(figure)
        raise ValueError("Week 3 parameter sweep has no batch or queue samples to plot")
    left.set(
        xlabel="Window / maximum wait (ms)",
        ylabel="Mean batch fill ratio",
        title="Batch fill versus queueing cost at 0.75 load",
    )
    right.set_ylabel("P95 queue delay (ms)")
    left.grid(alpha=0.3)
    lines = left.get_lines() + right.get_lines()
    left.legend(lines, [line.get_label() for line in lines], fontsize=7, ncol=2)
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _plot_queue_depth(events: Sequence[Mapping[str, object]], path: Path) -> None:
    overload = [
        row
        for row in events
        if float(str(row["offered_load_ratio"])) >= 1.05
        and str(row["measurement"]).lower() in {"true", "1"}
    ]
    if not overload:
        raise ValueError("Week 3 evidence has no overload events")
    grouped: dict[tuple[str, str], list[Mapping[str, object]]] = defaultdict(list)
    for row in overload:
        grouped[(str(row["policy"]), str(row["case_id"]))].append(row)
    selected: dict[str, list[Mapping[str, object]]] = {}
    for (policy, case), rows in sorted(grouped.items()):
        selected.setdefault(policy, rows)
    _, axis = plt.subplots(figsize=(10, 5))
    plotted = False
    for policy, rows in sorted(selected.items()):
        points = reconstruct_queue_depth(rows)
        if points:
            axis.step(
                [time / 1e9 for time, _ in points],
                [depth for _, depth in points],
                where="post",
                label=policy,
            )
            plotted = True
    if not plotted:
        plt.close()
        raise ValueError("Week 3 overload evidence has no queue-depth samples to plot")
    axis.set(
        xlabel="Case-relative time (s)",
        ylabel="Admitted queue depth",
        title="Queue depth over time at 1.05 offered load",
    )
    axis.grid(alpha=0.3)
    axis.legend()
    plt.tight_layout()
    plt.savefig(path, dpi=160)
    plt.close()


def write_figures(
    summaries: Sequence[Mapping[str, object]],
    events: Sequence[Mapping[str, object]],
    output_dir: str | Path,
) -> tuple[Path, ...]:
    frame = pd.DataFrame(summaries)
    if frame.empty:
        raise ValueError("cannot plot empty Week 3 summaries")
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    outputs = tuple(destination / name for name in FIGURE_NAMES)
    _plot_throughput(frame, outputs[0])
    _plot_ttft(frame, outputs[1])
    _plot_window(frame, outputs[2])
    _plot_queue_depth(events, outputs[3])
    return outputs


def analyze(
    config: Mapping[str, object],
    *,
    events_path: str | Path | None = None,
    batches_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    artifact_root: str | Path | None = None,
) -> list[dict[str, object]]:
    output = config.get("output")
    if not isinstance(output, Mapping):
        raise ValueError("Week 3 config output must be a mapping")
    if artifact_root is not None:
        if any(value is not None for value in (events_path, batches_path, output_dir)):
            raise ValueError(
                "artifact_root cannot be combined with individual Week 3 path overrides"
            )
        root = Path(artifact_root)
        selected_events = root / "raw" / "events.csv"
        selected_batches = root / "raw" / "batches.csv"
        metadata_path = root / "raw" / "run_metadata.json"
        summary_path = root / "summary.csv"
        analysis_path = root / "analysis.json"
        figures_dir = root / "figures"
    else:
        selected_events = Path(events_path or str(output["raw_events_csv"]))
        selected_batches = Path(batches_path or str(output["raw_batches_csv"]))
        metadata_path = Path(str(output["run_metadata"]))
        summary_path = Path(str(output["summary_csv"]))
        analysis_path = Path(str(output["analysis_json"]))
        figures_dir = Path(output_dir or str(output["figures_dir"]))

    metadata = read_json(metadata_path)
    if not isinstance(metadata, Mapping):
        raise ValueError("Week 3 run metadata must be a mapping")
    profile = str(metadata.get("profile", ""))
    backend = str(metadata.get("backend", ""))
    validate_run_metadata(metadata, config, profile=profile, backend=backend)
    events, events_digest = _read_csv_and_sha256(selected_events, EVENT_FIELDS)
    batches, batches_digest = _read_csv_and_sha256(selected_batches, BATCH_FIELDS)
    if backend == "hf" and (
        any(row["status"] == "failed" for row in events)
        or any(row["status"] == "failed" for row in batches)
    ):
        raise ValueError(
            "formal Week 3 HF evidence contains failed requests or batches"
        )
    summaries = summarize_results(events, batches, metadata=metadata)
    expected_figures = tuple(figures_dir / name for name in FIGURE_NAMES)
    payload = analysis_payload(
        summaries,
        metadata=metadata,
        events_sha256=events_digest,
        batches_sha256=batches_digest,
        figures=expected_figures,
    )
    write_text(summary_path, summary_csv_text(summaries))
    figures = write_figures(summaries, events, figures_dir)
    if figures != expected_figures:
        raise RuntimeError("Week 3 figure output paths are inconsistent")
    write_json(analysis_path, payload)
    return summaries


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze Week 3 batching evidence.")
    parser.add_argument("--config", default="configs/week03.yaml")
    parser.add_argument("--events")
    parser.add_argument("--batches")
    parser.add_argument("--output-dir")
    parser.add_argument(
        "--artifact-root",
        help="Use <root>/raw inputs and write all analysis artifacts below <root>",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summaries = analyze(
        load_yaml(args.config),
        events_path=args.events,
        batches_path=args.batches,
        output_dir=args.output_dir,
        artifact_root=args.artifact_root,
    )
    print(
        f"Analyzed {len(summaries)} Week 3 cases and wrote {len(FIGURE_NAMES)} figures"
    )


if __name__ == "__main__":
    main()
