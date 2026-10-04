from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from src.common import load_yaml, read_json, utc_now, write_json, write_text
from src.result_store import read_rows
from src.week02_contract import (
    RESULT_FIELDS,
    validate_canonical_matrix,
    validate_official_completion,
    validate_result_rows,
    validate_run_metadata,
)
from src.week02_smoke import validate_smoke_artifact, validate_smoke_formal_hashes

FIGURE_NAMES = (
    "prompt-length-vs-ttft.png",
    "output-length-vs-e2e-latency.png",
    "batch-size-vs-token-throughput.png",
    "batch-size-vs-latency-memory.png",
)
FIGURE_CONTRACT_VERSION = 1
SUMMARY_FIELDS = [
    "sweep",
    "batch_size",
    "prompt_tokens",
    "output_tokens",
    "point_status",
    "terminal_count",
    "completed_count",
    "oom_count",
    "error_count",
    "median_gpu_ttft_ms",
    "p95_gpu_ttft_ms",
    "median_mean_tpot_ms",
    "p95_mean_tpot_ms",
    "median_p95_itl_ms",
    "p95_p95_itl_ms",
    "median_generation_ms",
    "p95_generation_ms",
    "median_e2e_latency_ms",
    "p95_e2e_latency_ms",
    "median_output_tokens_per_second",
    "p95_output_tokens_per_second",
    "median_requests_per_second",
    "p95_requests_per_second",
    "median_memory_allocated_delta_mb",
    "p95_memory_allocated_delta_mb",
    "median_memory_reserved_delta_mb",
    "p95_memory_reserved_delta_mb",
    "theoretical_kv_cache_mib",
]
SUMMARY_METRICS = (
    "gpu_ttft_ms",
    "mean_tpot_ms",
    "p95_itl_ms",
    "generation_ms",
    "e2e_latency_ms",
    "output_tokens_per_second",
    "requests_per_second",
    "memory_allocated_delta_mb",
    "memory_reserved_delta_mb",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze the Week 2 batch sweeps.")
    parser.add_argument("--config", default="configs/week02.yaml")
    parser.add_argument("--input")
    parser.add_argument("--output-dir")
    parser.add_argument("--summary-csv")
    parser.add_argument("--analysis-json")
    return parser.parse_args()


def nearest_rank(values: Sequence[float], fraction: float) -> float:
    if not values:
        raise ValueError("nearest_rank requires at least one value")
    if not 0 < fraction <= 1:
        raise ValueError("fraction must be in (0, 1]")
    ordered = sorted(values)
    return ordered[max(math.ceil(fraction * len(ordered)) - 1, 0)]


def _group_key(row: Mapping[str, str]) -> tuple[str, int, int, int]:
    return (
        row["sweep"],
        int(row["batch_size"]),
        int(row["prompt_tokens"]),
        int(row["output_tokens"]),
    )


def summarize_results(rows: Iterable[Mapping[str, str]]) -> list[dict[str, object]]:
    """Summarize successful repeats without hiding terminal OOM/error counts."""

    grouped: dict[tuple[str, int, int, int], list[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[_group_key(row)].append(row)
    summary: list[dict[str, object]] = []
    for key, group in sorted(grouped.items()):
        successful = [row for row in group if row["status"] == "completed"]
        statuses = {row["status"] for row in group}
        point_status = (
            "completed"
            if statuses == {"completed"}
            else "capacity_limited"
            if statuses == {"oom"}
            else "invalid"
        )
        result: dict[str, object] = {
            "sweep": key[0],
            "batch_size": key[1],
            "prompt_tokens": key[2],
            "output_tokens": key[3],
            "point_status": point_status,
            "terminal_count": len(group),
            "completed_count": len(successful),
            "oom_count": sum(row["status"] == "oom" for row in group),
            "error_count": sum(row["status"] == "error" for row in group),
        }
        for metric in SUMMARY_METRICS:
            values = (
                [float(row[metric]) for row in successful]
                if point_status == "completed"
                else []
            )
            result[f"median_{metric}"] = (
                statistics.median(values) if values else ""
            )
            result[f"p95_{metric}"] = nearest_rank(values, 0.95) if values else ""
        kv_values = {float(row["theoretical_kv_cache_mib"]) for row in group}
        if len(kv_values) != 1:
            raise ValueError(f"Workload {key} has inconsistent theoretical KV cache values")
        result["theoretical_kv_cache_mib"] = kv_values.pop()
        summary.append(result)
    return summary


def summary_csv_text(summary: Sequence[Mapping[str, object]]) -> str:
    import io

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=SUMMARY_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in summary:
        if set(row) != set(SUMMARY_FIELDS):
            raise ValueError("Summary row does not match the Week 2 summary schema")
        writer.writerow(row)
    return buffer.getvalue()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_record(path: str | Path) -> dict[str, str]:
    candidate = Path(path)
    return {"path": str(candidate), "sha256": sha256_file(candidate)}


def figure_plot_input_sha256(
    summary: Sequence[Mapping[str, object]],
    metadata: Mapping[str, object],
    figure_name: str,
) -> str:
    if figure_name not in FIGURE_NAMES:
        raise ValueError(f"Unknown Week 2 figure: {figure_name}")
    runtime = metadata.get("runtime")
    if not isinstance(runtime, Mapping):
        raise ValueError("metadata.runtime must be a mapping")
    payload = {
        "figure_contract_version": FIGURE_CONTRACT_VERSION,
        "figure_name": figure_name,
        "summary": list(summary),
        "context": {
            "model": runtime.get("model"),
            "dtype": runtime.get("dtype"),
            "gpu_names": runtime.get("gpu_names"),
        },
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _successful(
    summary: Sequence[Mapping[str, object]], sweep: str
) -> list[Mapping[str, object]]:
    rows = [
        row
        for row in summary
        if row["sweep"] == sweep and int(row["completed_count"]) > 0
    ]
    if not rows:
        raise ValueError(
            f"Cannot generate the {sweep} figure: the sweep has no completed measurements"
        )
    return rows


def _sweep_rows(
    summary: Sequence[Mapping[str, object]], sweep: str
) -> list[Mapping[str, object]]:
    rows = [row for row in summary if row["sweep"] == sweep]
    if not rows:
        raise ValueError(f"Week 2 summary is missing the {sweep} sweep")
    return rows


def _context(metadata: Mapping[str, object]) -> str:
    runtime = metadata.get("runtime")
    if not isinstance(runtime, Mapping):
        raise ValueError("metadata.runtime must be a mapping")
    gpu_names = runtime.get("gpu_names")
    gpu = ", ".join(map(str, gpu_names)) if isinstance(gpu_names, list) else str(gpu_names)
    return f"{runtime.get('model')} | {runtime.get('dtype')} | {gpu}"


def _failure_note(rows: Sequence[Mapping[str, object]]) -> str:
    oom = sum(int(row["oom_count"]) for row in rows)
    errors = sum(int(row["error_count"]) for row in rows)
    failed_points = [
        (
            f"b={row['batch_size']},p={row['prompt_tokens']},o={row['output_tokens']}"
            f" (OOM={row['oom_count']}, error={row['error_count']})"
        )
        for row in rows
        if int(row["completed_count"]) == 0
    ]
    suffix = f"; no successful repeat: {', '.join(failed_points)}" if failed_points else ""
    return f"All terminal attempts: OOM={oom}, error={errors}{suffix}"


def generate_figures(
    summary: Sequence[Mapping[str, object]],
    metadata: Mapping[str, object],
    output_dir: Path,
) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "figure.dpi": 100,
            "savefig.dpi": 160,
            "axes.unicode_minus": False,
        }
    )
    png_metadata = {"Software": "adaptive-llm-serving-week02"}

    output_dir.mkdir(parents=True, exist_ok=True)
    context = _context(metadata)
    outputs: list[Path] = []

    all_prompt_rows = _sweep_rows(summary, "prompt_length")
    prompt_rows = sorted(
        _successful(summary, "prompt_length"),
        key=lambda row: int(row["prompt_tokens"]),
    )
    figure, axis = plt.subplots(figsize=(9, 5))
    axis.plot(
        [int(row["prompt_tokens"]) for row in prompt_rows],
        [float(row["median_gpu_ttft_ms"]) for row in prompt_rows],
        marker="o",
        label="median GPU TTFT",
    )
    axis.plot(
        [int(row["prompt_tokens"]) for row in prompt_rows],
        [float(row["p95_gpu_ttft_ms"]) for row in prompt_rows],
        marker="x",
        linestyle="--",
        label="nearest-rank P95 GPU TTFT",
    )
    axis.set(
        xlabel="Prompt tokens",
        ylabel="GPU TTFT (ms)",
        title=f"Prompt length vs TTFT (batch=1, output=64)\n{context}",
    )
    axis.grid(alpha=0.3)
    axis.legend()
    figure.text(0.01, 0.01, _failure_note(all_prompt_rows), fontsize=8)
    figure.tight_layout(rect=(0, 0.04, 1, 1))
    path = output_dir / FIGURE_NAMES[0]
    figure.savefig(path, dpi=160, metadata=png_metadata)
    plt.close(figure)
    outputs.append(path)

    all_output_rows = _sweep_rows(summary, "output_length")
    output_rows = sorted(
        _successful(summary, "output_length"),
        key=lambda row: int(row["output_tokens"]),
    )
    figure, axis = plt.subplots(figsize=(9, 5))
    axis.plot(
        [int(row["output_tokens"]) for row in output_rows],
        [float(row["median_e2e_latency_ms"]) for row in output_rows],
        marker="o",
        label="median E2E latency",
    )
    axis.plot(
        [int(row["output_tokens"]) for row in output_rows],
        [float(row["p95_e2e_latency_ms"]) for row in output_rows],
        marker="x",
        linestyle="--",
        label="nearest-rank P95 E2E latency",
    )
    axis.set(
        xlabel="Requested output tokens per request",
        ylabel="Batch completion E2E latency (ms)",
        title=f"Output length vs E2E latency (batch=1, prompt=256)\n{context}",
    )
    axis.grid(alpha=0.3)
    axis.legend()
    figure.text(0.01, 0.01, _failure_note(all_output_rows), fontsize=8)
    figure.tight_layout(rect=(0, 0.04, 1, 1))
    path = output_dir / FIGURE_NAMES[1]
    figure.savefig(path, dpi=160, metadata=png_metadata)
    plt.close(figure)
    outputs.append(path)

    all_batch_rows = _sweep_rows(summary, "batch_size")
    batch_rows = sorted(
        _successful(summary, "batch_size"),
        key=lambda row: int(row["batch_size"]),
    )
    figure, axis = plt.subplots(figsize=(9, 5))
    axis.plot(
        [int(row["batch_size"]) for row in batch_rows],
        [float(row["median_output_tokens_per_second"]) for row in batch_rows],
        marker="o",
        label="median aggregate output throughput",
    )
    axis.plot(
        [int(row["batch_size"]) for row in batch_rows],
        [float(row["p95_output_tokens_per_second"]) for row in batch_rows],
        marker="x",
        linestyle="--",
        label="nearest-rank P95 aggregate throughput",
    )
    axis.set(
        xlabel="Static batch size",
        ylabel="Aggregate output tokens/s",
        title=f"Batch size vs output throughput (prompt=256, output=64)\n{context}",
    )
    axis.set_xticks([1, 2, 4, 8, 16])
    axis.grid(alpha=0.3)
    axis.legend()
    figure.text(0.01, 0.01, _failure_note(all_batch_rows), fontsize=8)
    figure.tight_layout(rect=(0, 0.04, 1, 1))
    path = output_dir / FIGURE_NAMES[2]
    figure.savefig(path, dpi=160, metadata=png_metadata)
    plt.close(figure)
    outputs.append(path)

    figure, latency_axis = plt.subplots(figsize=(9, 5))
    memory_axis = latency_axis.twinx()
    x_values = [int(row["batch_size"]) for row in batch_rows]
    latency_line = latency_axis.plot(
        x_values,
        [float(row["median_e2e_latency_ms"]) for row in batch_rows],
        color="tab:blue",
        marker="o",
        label="median E2E latency",
    )
    allocated_line = memory_axis.plot(
        x_values,
        [float(row["median_memory_allocated_delta_mb"]) for row in batch_rows],
        color="tab:red",
        marker="s",
        label="median allocated delta",
    )
    kv_line = memory_axis.plot(
        x_values,
        [float(row["theoretical_kv_cache_mib"]) for row in batch_rows],
        color="tab:green",
        marker="^",
        linestyle="--",
        label="theoretical KV tensors",
    )
    latency_axis.set(
        xlabel="Static batch size",
        ylabel="Batch completion E2E latency (ms)",
        title=f"Batch latency and memory (prompt=256, output=64)\n{context}",
    )
    memory_axis.set_ylabel("Memory relative to loaded-model baseline (MiB)")
    latency_axis.set_xticks([1, 2, 4, 8, 16])
    latency_axis.grid(alpha=0.3)
    lines = latency_line + allocated_line + kv_line
    latency_axis.legend(lines, [line.get_label() for line in lines], loc="upper left")
    figure.text(0.01, 0.01, _failure_note(all_batch_rows), fontsize=8)
    figure.tight_layout(rect=(0, 0.04, 1, 1))
    path = output_dir / FIGURE_NAMES[3]
    figure.savefig(path, dpi=160, metadata=png_metadata)
    plt.close(figure)
    outputs.append(path)
    return outputs


def main() -> None:
    args = parse_args()
    config = load_yaml(args.config)
    validate_canonical_matrix(config)
    output = config["output"]
    input_path = Path(args.input or output["raw_csv"])
    metadata_path = Path(output["run_metadata"])
    smoke_path = Path(output["smoke_json"])
    output_dir = Path(args.output_dir or output["figures_dir"])
    summary_path = Path(args.summary_csv or output["summary_csv"])
    analysis_path = Path(args.analysis_json or output["analysis_json"])
    metadata = read_json(metadata_path)
    validate_run_metadata(metadata, config)
    smoke = read_json(smoke_path)
    validate_smoke_artifact(smoke, config, metadata)
    rows = read_rows(input_path, expected_fields=RESULT_FIELDS)
    validate_result_rows(rows, metadata, config, require_complete=True)
    validate_smoke_formal_hashes(smoke, rows)
    validate_official_completion(rows, config)
    summary = summarize_results(rows)
    write_text(summary_path, summary_csv_text(summary))
    figure_paths = generate_figures(summary, metadata, output_dir)
    status_counts = {
        status: sum(row["status"] == status for row in rows)
        for status in ("completed", "oom", "error")
    }
    figure_records = {}
    for path in figure_paths:
        record = artifact_record(path)
        record["plot_input_sha256"] = figure_plot_input_sha256(
            summary, metadata, path.name
        )
        figure_records[path.name] = record
    payload = {
        "schema_version": 2,
        "generated_at": utc_now(),
        "run_id": metadata["run_id"],
        "source_tree_fingerprint": metadata["source"]["source_tree_fingerprint"],
        "inputs": {
            "raw_csv": artifact_record(input_path),
            "run_metadata": artifact_record(metadata_path),
            "smoke_json": artifact_record(smoke_path),
        },
        "outputs": {
            "summary_csv": artifact_record(summary_path),
            "figures": figure_records,
        },
        "terminal_case_count": len(rows),
        "status_counts": status_counts,
        "aggregation": {
            "center": "median over completed repeats",
            "tail": "nearest-rank P95 over completed repeats",
            "capacity_policy": (
                "all configured repeats completed, or all measured attempts OOM "
                "at a monotonic sweep suffix; capacity-limited points have no "
                "latency or throughput aggregate"
            ),
        },
    }
    write_json(analysis_path, payload)
    print(
        f"Wrote {len(figure_paths)} Week 2 figures, {summary_path}, and {analysis_path}; "
        f"status counts={status_counts}."
    )


if __name__ == "__main__":
    main()
