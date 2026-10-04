from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from src.analyze_week02 import (
    generate_figures,
    figure_plot_input_sha256,
    nearest_rank,
    sha256_file,
    summarize_results,
    summary_csv_text,
)


def row(
    *,
    repeat: int,
    status: str = "completed",
    ttft: float = 10.0,
    latency: float = 20.0,
    throughput: float = 100.0,
) -> dict[str, str]:
    result = {
        "sweep": "batch_size",
        "batch_size": "2",
        "prompt_tokens": "256",
        "output_tokens": "64",
        "repeat": str(repeat),
        "status": status,
        "gpu_ttft_ms": "",
        "mean_tpot_ms": "",
        "p95_itl_ms": "",
        "generation_ms": "",
        "e2e_latency_ms": "",
        "output_tokens_per_second": "",
        "requests_per_second": "",
        "memory_allocated_delta_mb": "",
        "memory_reserved_delta_mb": "",
        "theoretical_kv_cache_mib": "4.0",
    }
    if status == "completed":
        result.update(
            {
                "gpu_ttft_ms": str(ttft),
                "mean_tpot_ms": "2.0",
                "p95_itl_ms": "3.0",
                "generation_ms": str(latency - 2),
                "e2e_latency_ms": str(latency),
                "output_tokens_per_second": str(throughput),
                "requests_per_second": "1.5",
                "memory_allocated_delta_mb": "50.0",
                "memory_reserved_delta_mb": "64.0",
            }
        )
    return result


def test_nearest_rank_p95_uses_observed_value() -> None:
    assert nearest_rank([10, 20, 30, 40, 50], 0.95) == 50


def test_summary_uses_successes_and_preserves_failure_counts() -> None:
    rows = [
        row(repeat=0, ttft=10, latency=20, throughput=100),
        row(repeat=1, ttft=30, latency=40, throughput=200),
        row(repeat=2, status="oom"),
        row(repeat=3, status="error"),
    ]

    summary = summarize_results(rows)

    assert len(summary) == 1
    result = summary[0]
    assert result["terminal_count"] == 4
    assert result["completed_count"] == 2
    assert result["oom_count"] == 1
    assert result["error_count"] == 1
    assert result["point_status"] == "invalid"
    assert result["median_gpu_ttft_ms"] == ""
    assert result["p95_gpu_ttft_ms"] == ""
    assert result["median_output_tokens_per_second"] == ""


def test_all_failed_workload_has_blank_numeric_aggregates() -> None:
    summary = summarize_results(
        [row(repeat=0, status="oom"), row(repeat=1, status="oom")]
    )[0]

    assert summary["completed_count"] == 0
    assert summary["oom_count"] == 2
    assert summary["point_status"] == "capacity_limited"
    assert summary["median_gpu_ttft_ms"] == ""
    assert summary["p95_output_tokens_per_second"] == ""


def test_summary_csv_is_stable_and_parseable() -> None:
    summary = summarize_results([row(repeat=0)])
    serialized = summary_csv_text(summary)
    parsed = list(csv.DictReader(io.StringIO(serialized)))

    assert len(parsed) == 1
    assert parsed[0]["sweep"] == "batch_size"
    assert parsed[0]["median_gpu_ttft_ms"] == "10.0"


def test_inconsistent_theoretical_kv_value_is_rejected() -> None:
    first = row(repeat=0)
    second = row(repeat=1)
    second["theoretical_kv_cache_mib"] = "5.0"

    with pytest.raises(ValueError, match="inconsistent theoretical"):
        summarize_results([first, second])


def _figure_summary() -> list[dict[str, object]]:
    rows: list[dict[str, str]] = []
    for sweep, batch_size, prompt_tokens, output_tokens in (
        ("prompt_length", 1, 32, 64),
        ("output_length", 1, 256, 16),
        ("batch_size", 1, 256, 64),
    ):
        item = row(repeat=0)
        item.update(
            {
                "sweep": sweep,
                "batch_size": str(batch_size),
                "prompt_tokens": str(prompt_tokens),
                "output_tokens": str(output_tokens),
            }
        )
        rows.append(item)
    return summarize_results(rows)


def test_figure_generation_is_byte_reproducible(tmp_path: Path) -> None:
    metadata = {
        "runtime": {
            "model": "example/model",
            "dtype": "bfloat16",
            "gpu_names": ["Fake GPU"],
        }
    }
    first = generate_figures(_figure_summary(), metadata, tmp_path / "first")
    second = generate_figures(_figure_summary(), metadata, tmp_path / "second")

    assert [sha256_file(path) for path in first] == [sha256_file(path) for path in second]
    first_hash = figure_plot_input_sha256(
        _figure_summary(), metadata, "prompt-length-vs-ttft.png"
    )
    changed = _figure_summary()
    changed[0]["median_gpu_ttft_ms"] = 999.0
    assert (
        figure_plot_input_sha256(changed, metadata, "prompt-length-vs-ttft.png")
        != first_hash
    )
