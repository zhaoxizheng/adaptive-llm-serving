from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from src.week01_contract import (
    RESULT_FIELDS,
    config_fingerprint,
    create_run_metadata,
    expected_cases,
    scientific_config,
    validate_result_rows,
    validate_run_metadata,
)

PINNED_REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"


def make_config() -> dict[str, Any]:
    return {
        "model": {
            "id": "example/tiny-model",
            "revision": PINNED_REVISION,
            "dtype": "bfloat16",
            "local_files_only": True,
        },
        "generation": {"prompt": "A deterministic prompt", "seed": 42},
        "benchmark": {
            "warmup_runs": 1,
            "repeats": 1,
            "prompt_tokens": [4],
            "output_tokens": [2],
            "cache_modes": [True, False],
        },
        "output": {
            "raw_csv": "results/week01/raw/kv_cache.csv",
            "run_metadata": "results/week01/raw/run_metadata.json",
            "figures_dir": "results/week01/figures",
        },
    }


def make_source() -> dict[str, object]:
    return {
        "git_commit": "a" * 40,
        "git_dirty": False,
        "critical_source_dirty": False,
        "dirty_state_fingerprint": "b" * 64,
        "source_tree_fingerprint": "e" * 64,
        "source": "git",
    }


def make_runtime() -> dict[str, object]:
    return {
        "python": "3.12.7",
        "python_implementation": "CPython",
        "platform": "Linux",
        "pytorch": "2.8.0+cu128",
        "transformers": "4.46.3",
        "cuda_runtime": "12.8",
        "driver": "570.00",
        "gpu_names": ["NVIDIA L4"],
        "gpu_capabilities": [[8, 9]],
        "gpu_count": 1,
        "dtype": "bfloat16",
        "model": "example/tiny-model",
        "model_revision": PINNED_REVISION,
    }


def make_metadata(
    config: dict[str, Any] | None = None,
    source: dict[str, object] | None = None,
    runtime: dict[str, object] | None = None,
) -> dict[str, object]:
    return create_run_metadata(
        config or make_config(),
        source or make_source(),
        runtime or make_runtime(),
    )


def make_row(
    metadata: dict[str, object],
    *,
    use_cache: bool = True,
    repeat: int = 0,
    output_hash: str | None = None,
) -> dict[str, str]:
    source = metadata["source"]
    runtime = metadata["runtime"]
    assert isinstance(source, dict)
    assert isinstance(runtime, dict)
    row = dict.fromkeys(RESULT_FIELDS, "")
    row.update(
        {
            "timestamp": "2026-09-25T12:00:00+00:00",
            "run_id": str(metadata["run_id"]),
            "git_commit": str(source["git_commit"]),
            "config_fingerprint": str(metadata["config_fingerprint"]),
            "runtime_fingerprint": str(metadata["runtime_fingerprint"]),
            "model": str(runtime["model"]),
            "model_revision": str(runtime["model_revision"]),
            "dtype": str(runtime["dtype"]),
            "repeat": str(repeat),
            "use_cache": str(use_cache).lower(),
            "prompt_tokens": "4",
            "output_tokens": "2",
            "tokenization_ms": "1.0",
            "h2d_ms": "0.25",
            "prefill_forward_ms": "1.5",
            "first_token_selection_ms": "0.5",
            "inference_ttft_ms": "2.25",
            "end_to_end_ttft_ms": "3.25",
            "decode_ms": "6.0",
            "mean_tpot_ms": "6.0",
            "p50_tpot_ms": "6.0",
            "p95_tpot_ms": "6.0",
            "total_generation_ms": "8.25",
            "end_to_end_ms": "9.25",
            "output_tokens_per_second": str(2 / 0.00825),
            "peak_memory_mb": "128.0",
            "output_token_hash": output_hash or f"{repeat + 1:016x}",
        }
    )
    return row


def test_config_fingerprint_excludes_output_paths() -> None:
    config = make_config()
    relocated = deepcopy(config)
    relocated["output"] = {
        "raw_csv": "/mnt/evidence/results.csv",
        "run_metadata": "/mnt/evidence/metadata.json",
        "figures_dir": "/mnt/evidence/figures",
    }

    assert scientific_config(config) == scientific_config(relocated)
    assert "output" not in scientific_config(config)
    assert config_fingerprint(config) == config_fingerprint(relocated)


def test_config_fingerprint_changes_with_scientific_inputs() -> None:
    config = make_config()
    changed = deepcopy(config)
    changed["generation"]["seed"] = 43

    assert config_fingerprint(config) != config_fingerprint(changed)


@pytest.mark.parametrize(
    "revision",
    [
        "main",
        "a" * 39,
        "a" * 41,
        "g" * 40,
        "A" * 40,
    ],
)
def test_config_fingerprint_rejects_nonimmutable_model_revision(revision: str) -> None:
    config = make_config()
    config["model"]["revision"] = revision

    with pytest.raises(ValueError, match="immutable 40-character"):
        config_fingerprint(config)


def test_scientific_config_accepts_exact_lowercase_commit_sha() -> None:
    config = make_config()

    assert scientific_config(config)["model"]["revision"] == PINNED_REVISION


def test_matching_metadata_validates_for_resume_after_output_relocation() -> None:
    config = make_config()
    source = make_source()
    runtime = make_runtime()
    metadata = make_metadata(config, source, runtime)
    relocated = deepcopy(config)
    relocated["output"]["raw_csv"] = "/mnt/resumed/results.csv"

    validate_run_metadata(metadata, relocated, source=source, runtime=runtime)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("git_commit", "c" * 40),
        ("git_dirty", True),
        ("critical_source_dirty", True),
        ("dirty_state_fingerprint", "d" * 64),
        ("source_tree_fingerprint", "f" * 64),
    ],
)
def test_metadata_resume_rejects_changed_source_identity(
    field: str, replacement: object
) -> None:
    config = make_config()
    source = make_source()
    runtime = make_runtime()
    metadata = make_metadata(config, source, runtime)
    changed_source = {**source, field: replacement}

    with pytest.raises(ValueError, match="Git source identity differs"):
        validate_run_metadata(metadata, config, source=changed_source, runtime=runtime)


def test_metadata_resume_rejects_changed_runtime_identity() -> None:
    config = make_config()
    source = make_source()
    runtime = make_runtime()
    metadata = make_metadata(config, source, runtime)
    changed_runtime = {**runtime, "driver": "571.00"}

    with pytest.raises(ValueError, match="runtime identity changed"):
        validate_run_metadata(metadata, config, source=source, runtime=changed_runtime)


def test_metadata_resume_rejects_changed_scientific_config() -> None:
    config = make_config()
    metadata = make_metadata(config)
    changed_config = deepcopy(config)
    changed_config["benchmark"]["repeats"] = 2

    with pytest.raises(ValueError, match="current scientific config"):
        validate_run_metadata(metadata, changed_config)


def test_duplicate_result_case_normalizes_boolean_spellings() -> None:
    config = make_config()
    metadata = make_metadata(config)
    duplicate = make_row(metadata, use_cache=True)
    duplicate["use_cache"] = "1"

    with pytest.raises(ValueError, match="Duplicate result case"):
        validate_result_rows(
            [make_row(metadata, use_cache=True), duplicate],
            metadata,
            config,
            require_complete=False,
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("run_id", "00000000-0000-0000-0000-000000000000"),
        ("git_commit", "c" * 40),
        ("config_fingerprint", "different-config"),
        ("runtime_fingerprint", "different-runtime"),
        ("model", "example/other-model"),
        ("model_revision", "d" * 40),
        ("dtype", "float16"),
    ],
)
def test_mixed_result_identity_is_rejected(field: str, replacement: str) -> None:
    config = make_config()
    metadata = make_metadata(config)
    mixed_row = make_row(metadata, use_cache=False)
    mixed_row[field] = replacement

    with pytest.raises(ValueError, match=rf"identity mismatch for {field}"):
        validate_result_rows(
            [make_row(metadata, use_cache=True), mixed_row],
            metadata,
            config,
            require_complete=False,
        )


def test_cache_modes_must_produce_the_same_output_tokens() -> None:
    config = make_config()
    metadata = make_metadata(config)

    with pytest.raises(ValueError, match="Cache on/off output mismatch"):
        validate_result_rows(
            [
                make_row(metadata, use_cache=True, output_hash="1" * 16),
                make_row(metadata, use_cache=False, output_hash="2" * 16),
            ],
            metadata,
            config,
            require_complete=False,
        )


def test_partial_matrix_is_valid_only_for_resume() -> None:
    config = make_config()
    metadata = make_metadata(config)
    row = make_row(metadata, use_cache=True)

    assert validate_result_rows(
        [row], metadata, config, require_complete=False
    ) == {(4, 2, 0, True)}
    with pytest.raises(ValueError, match=r"matrix is incomplete: 1/2 cases"):
        validate_result_rows([row], metadata, config, require_complete=True)


def test_complete_matrix_returns_every_expected_case() -> None:
    config = make_config()
    metadata = make_metadata(config)
    rows = [
        make_row(metadata, use_cache=True),
        make_row(metadata, use_cache=False),
    ]

    assert validate_result_rows(
        rows, metadata, config, require_complete=True
    ) == expected_cases(config)


def test_latency_contract_has_unambiguous_decomposition() -> None:
    from src.latency import summarize_latency

    result = summarize_latency(
        tokenization_ms=3.0,
        h2d_ms=1.0,
        prefill_forward_ms=8.0,
        first_token_selection_ms=1.0,
        decode_step_ms=[2.0, 4.0, 8.0],
        output_tokens=4,
    )

    assert result == {
        "inference_ttft_ms": 10.0,
        "end_to_end_ttft_ms": 13.0,
        "decode_ms": 14.0,
        "mean_tpot_ms": 14.0 / 3,
        "p50_tpot_ms": 4.0,
        "p95_tpot_ms": 8.0,
        "total_generation_ms": 24.0,
        "end_to_end_ms": 27.0,
        "output_tokens_per_second": 4 / 0.024,
    }


def test_one_output_token_has_no_tpot_observation() -> None:
    from src.latency import summarize_latency

    result = summarize_latency(
        tokenization_ms=3.0,
        h2d_ms=1.0,
        prefill_forward_ms=8.0,
        first_token_selection_ms=1.0,
        decode_step_ms=[],
        output_tokens=1,
    )

    assert result["decode_ms"] == 0.0
    assert result["mean_tpot_ms"] is None
    assert result["p50_tpot_ms"] is None
    assert result["p95_tpot_ms"] is None
    assert result["total_generation_ms"] == 10.0


def test_result_validation_rejects_inconsistent_ttft_decomposition() -> None:
    config = make_config()
    metadata = make_metadata(config)
    row = make_row(metadata)
    row["inference_ttft_ms"] = "2.5"
    row["end_to_end_ttft_ms"] = "3.5"
    row["total_generation_ms"] = "8.5"
    row["end_to_end_ms"] = "9.5"
    row["output_tokens_per_second"] = str(2 / 0.0085)

    with pytest.raises(ValueError, match="inconsistent inference_ttft_ms"):
        validate_result_rows([row], metadata, config, require_complete=False)
