from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from src.week02_contract import (
    RESULT_FIELDS,
    case_key,
    config_fingerprint,
    create_run_metadata,
    expected_cases,
    iter_case_specs,
    validate_canonical_matrix,
    validate_official_completion,
    validate_result_rows,
    validate_run_metadata,
)

PINNED_REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"


def make_config(repeats: int = 1) -> dict[str, Any]:
    return {
        "model": {
            "id": "example/tiny-model",
            "revision": PINNED_REVISION,
            "dtype": "bfloat16",
            "local_files_only": True,
        },
        "generation": {
            "prompt": "controlled",
            "seed": 42,
            "decoding": "greedy",
            "padding_side": "left",
        },
        "benchmark": {
            "warmup_runs": 1,
            "repeats": repeats,
            "use_cache": True,
            "smoke_batch_sizes": [1, 2, 4],
            "sweeps": {
                "prompt_length": {
                    "batch_size": 1,
                    "prompt_tokens": [4],
                    "output_tokens": 3,
                },
                "batch_size": {
                    "batch_size": [1, 2],
                    "prompt_tokens": 4,
                    "output_tokens": 3,
                },
            },
        },
        "output": {"raw_csv": "elsewhere.csv"},
    }


def canonical_config() -> dict[str, Any]:
    config = make_config(repeats=5)
    config["benchmark"]["warmup_runs"] = 2
    config["benchmark"]["sweeps"] = {
        "prompt_length": {
            "batch_size": 1,
            "prompt_tokens": [32, 256, 1024, 2048],
            "output_tokens": 64,
        },
        "output_length": {
            "batch_size": 1,
            "prompt_tokens": 256,
            "output_tokens": [16, 64, 256],
        },
        "batch_size": {
            "batch_size": [1, 2, 4, 8, 16],
            "prompt_tokens": 256,
            "output_tokens": 64,
        },
    }
    return config


def make_source() -> dict[str, object]:
    return {
        "git_commit": "a" * 40,
        "git_dirty": False,
        "critical_source_dirty": False,
        "dirty_state_fingerprint": "b" * 64,
        "source_tree_fingerprint": "c" * 64,
    }


def make_runtime() -> dict[str, object]:
    return {
        "model": "example/tiny-model",
        "model_revision": PINNED_REVISION,
        "dtype": "bfloat16",
        "gpu_names": ["Fake GPU"],
    }


def make_row(
    metadata: dict[str, object],
    *,
    sweep: str = "prompt_length",
    batch_size: int = 1,
    repeat: int = 0,
    status: str = "completed",
) -> dict[str, str]:
    source = metadata["source"]
    runtime = metadata["runtime"]
    assert isinstance(source, dict)
    assert isinstance(runtime, dict)
    output_tokens = 3
    generation_ms = 15.0
    baseline_allocated = 100 * 1024**2
    baseline_reserved = 120 * 1024**2
    peak_allocated = 112 * 1024**2
    peak_reserved = 140 * 1024**2
    theoretical = 2 * 1024**2
    row = dict.fromkeys(RESULT_FIELDS, "")
    row.update(
        {
            "timestamp": "2026-10-03T12:00:00+00:00",
            "run_id": str(metadata["run_id"]),
            "git_commit": str(source["git_commit"]),
            "config_fingerprint": str(metadata["config_fingerprint"]),
            "runtime_fingerprint": str(metadata["runtime_fingerprint"]),
            "model": str(runtime["model"]),
            "model_revision": str(runtime["model_revision"]),
            "dtype": str(runtime["dtype"]),
            "sweep": sweep,
            "case_name": f"{sweep}-b{batch_size:02d}-p0004-o003-r{repeat:02d}",
            "repeat": str(repeat),
            "batch_size": str(batch_size),
            "prompt_tokens": "4",
            "output_tokens": str(output_tokens),
            "use_cache": "true",
            "status": status,
            "model_baseline_allocated_bytes": str(baseline_allocated),
            "model_baseline_reserved_bytes": str(baseline_reserved),
            "peak_memory_allocated_bytes": str(peak_allocated),
            "peak_memory_reserved_bytes": str(peak_reserved),
            "memory_allocated_delta_bytes": str(peak_allocated - baseline_allocated),
            "memory_reserved_delta_bytes": str(peak_reserved - baseline_reserved),
            "model_baseline_allocated_mb": "100.0",
            "model_baseline_reserved_mb": "120.0",
            "peak_memory_allocated_mb": "112.0",
            "peak_memory_reserved_mb": "140.0",
            "memory_allocated_delta_mb": "12.0",
            "memory_reserved_delta_mb": "20.0",
            "theoretical_kv_cache_bytes": str(theoretical),
            "theoretical_kv_cache_mib": "2.0",
        }
    )
    if status == "completed":
        row.update(
            {
                "actual_output_tokens": str(batch_size * output_tokens),
                "preprocessing_ms": "2.0",
                "h2d_ms": "1.0",
                "gpu_ttft_ms": "5.0",
                "e2e_ttft_ms": "8.0",
                "mean_tpot_ms": "5.0",
                "p95_itl_ms": "6.0",
                "generation_ms": str(generation_ms),
                "e2e_latency_ms": "18.0",
                "output_tokens_per_second": str(
                    batch_size * output_tokens / (generation_ms / 1_000)
                ),
                "requests_per_second": str(batch_size / (generation_ms / 1_000)),
                "output_token_hash": "0" * 64,
            }
        )
    else:
        row.update(
            {
                "error_phase": "measurement",
                "error_type": "OutOfMemoryError",
                "error_message": "CUDA out of memory",
            }
        )
    return row


def test_canonical_matrix_has_exact_60_named_cases() -> None:
    config = canonical_config()

    validate_canonical_matrix(config)
    specs = iter_case_specs(config)

    assert len(specs) == 60
    assert len({spec.name for spec in specs}) == 60
    assert len(expected_cases(config)) == 60
    assert sum(spec.sweep == "prompt_length" for spec in specs) == 20
    assert sum(spec.sweep == "output_length" for spec in specs) == 15
    assert sum(spec.sweep == "batch_size" for spec in specs) == 25


def test_canonical_matrix_rejects_one_missing_batch() -> None:
    config = canonical_config()
    config["benchmark"]["sweeps"]["batch_size"]["batch_size"] = [1, 2, 4, 8]

    with pytest.raises(ValueError, match="exact named 60-case"):
        validate_canonical_matrix(config)


@pytest.mark.parametrize(
    "smoke_sizes",
    ([1, 2], [1, 2, 2], [2, 4, 1], [0, 1, 2]),
)
def test_canonical_matrix_rejects_invalid_smoke_sizes(smoke_sizes: list[int]) -> None:
    config = canonical_config()
    config["benchmark"]["smoke_batch_sizes"] = smoke_sizes

    with pytest.raises(ValueError, match="smoke"):
        validate_canonical_matrix(config)


def test_config_fingerprint_excludes_output_paths() -> None:
    config = make_config()
    relocated = deepcopy(config)
    relocated["output"] = {"raw_csv": "/other/path.csv"}

    assert config_fingerprint(config) == config_fingerprint(relocated)


def test_completed_row_and_terminal_oom_both_count_for_resume() -> None:
    config = make_config()
    metadata = create_run_metadata(config, make_source(), make_runtime())
    completed = make_row(metadata)
    oom = make_row(metadata, sweep="batch_size", batch_size=2, status="oom")

    seen = validate_result_rows(
        [completed, oom], metadata, config, require_complete=False
    )

    assert case_key(completed) in seen
    assert case_key(oom) in seen


def test_terminal_failure_must_not_contain_fabricated_metrics() -> None:
    config = make_config()
    metadata = create_run_metadata(config, make_source(), make_runtime())
    oom = make_row(metadata, status="oom")
    oom["generation_ms"] = "1.0"

    with pytest.raises(ValueError, match="fabricated metrics"):
        validate_result_rows([oom], metadata, config, require_complete=False)


def test_completed_row_validates_latency_and_throughput_equations() -> None:
    config = make_config()
    metadata = create_run_metadata(config, make_source(), make_runtime())
    row = make_row(metadata)
    row["output_tokens_per_second"] = "1.0"

    with pytest.raises(ValueError, match="output token throughput"):
        validate_result_rows([row], metadata, config, require_complete=False)


def test_resume_rejects_changed_source_or_runtime() -> None:
    config = make_config()
    metadata = create_run_metadata(config, make_source(), make_runtime())

    with pytest.raises(ValueError, match="source identity differs"):
        validate_run_metadata(
            metadata,
            config,
            source={**make_source(), "git_commit": "f" * 40},
            runtime=make_runtime(),
        )


def test_result_hash_must_match_across_repeats_and_overlapping_sweeps() -> None:
    config = make_config(repeats=2)
    metadata = create_run_metadata(config, make_source(), make_runtime())
    first = make_row(metadata, repeat=0)
    second = make_row(metadata, repeat=1)
    second["output_token_hash"] = "1" * 64

    with pytest.raises(ValueError, match="Deterministic output mismatch"):
        validate_result_rows([first, second], metadata, config, require_complete=False)

    overlap = make_row(metadata, sweep="batch_size", batch_size=1, repeat=0)
    overlap["output_token_hash"] = "2" * 64
    with pytest.raises(ValueError, match="Deterministic output mismatch"):
        validate_result_rows([first, overlap], metadata, config, require_complete=False)


def test_result_hash_requires_full_sha256() -> None:
    config = make_config()
    metadata = create_run_metadata(config, make_source(), make_runtime())
    row = make_row(metadata)
    row["output_token_hash"] = "0" * 16

    with pytest.raises(ValueError, match="invalid output_token_hash"):
        validate_result_rows([row], metadata, config, require_complete=False)


def complete_rows(config: dict[str, Any], metadata: dict[str, object]) -> list[dict[str, str]]:
    return [
        make_row(
            metadata,
            sweep=case.sweep,
            batch_size=case.batch_size,
            repeat=case.repeat,
        )
        for case in iter_case_specs(config)
    ]


def test_official_completion_rejects_error_and_mixed_oom() -> None:
    config = make_config(repeats=2)
    metadata = create_run_metadata(config, make_source(), make_runtime())
    rows = complete_rows(config, metadata)
    rows[-1].update(
        make_row(
            metadata,
            sweep="batch_size",
            batch_size=2,
            repeat=1,
            status="error",
        )
    )
    rows[-1]["error_type"] = "RuntimeError"
    rows[-1]["error_message"] = "unexpected"
    with pytest.raises(ValueError, match="unexpected error"):
        validate_official_completion(rows, config)

    rows[-1] = make_row(
        metadata, sweep="batch_size", batch_size=2, repeat=1, status="oom"
    )
    with pytest.raises(ValueError, match="mixes completed and OOM"):
        validate_official_completion(rows, config)


def test_official_completion_accepts_monotonic_all_oom_suffix() -> None:
    config = make_config(repeats=2)
    metadata = create_run_metadata(config, make_source(), make_runtime())
    rows = complete_rows(config, metadata)
    rows = [
        (
            make_row(
                metadata,
                sweep="batch_size",
                batch_size=2,
                repeat=int(row["repeat"]),
                status="oom",
            )
            if row["sweep"] == "batch_size" and row["batch_size"] == "2"
            else row
        )
        for row in rows
    ]

    states = validate_official_completion(rows, config)

    assert states[("batch_size", 2, 4, 3)] == "capacity_limited"
