from __future__ import annotations

import csv
import json
import statistics
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from src.common import stable_fingerprint
from src.week02_contract import (
    RESULT_FIELDS,
    CaseSpec,
    create_run_metadata,
    iter_case_specs,
)
from src.week03_calibration import (
    CALIBRATION_METHOD,
    build_calibration_artifact,
    calibration_fingerprint,
    generate_calibration_artifact_file,
    load_calibration_artifact,
    validate_calibration_artifact,
)

PINNED_REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"
RAW_SHA256 = "d" * 64


def week2_config(*, prompt_tokens: int = 256) -> dict[str, Any]:
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
            "warmup_runs": 2,
            "repeats": 5,
            "use_cache": True,
            "smoke_batch_sizes": [1, 2, 4],
            "sweeps": {
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
                    "prompt_tokens": prompt_tokens,
                    "output_tokens": 64,
                }
            },
        },
        "output": {
            "run_metadata": "unused-metadata.json",
            "raw_csv": "unused.csv",
        },
    }


def week3_config(artifact: str = "results/week03/calibration.json") -> dict[str, Any]:
    return {
        "model": {
            "id": "example/tiny-model",
            "revision": PINNED_REVISION,
            "dtype": "bfloat16",
            "local_files_only": True,
        },
        "workload": {"prompt_tokens": 256, "output_tokens": 64},
        "calibration": {
            "artifact": artifact,
            "method": CALIBRATION_METHOD,
            "static_batch_sizes": [1, 2, 4, 8],
        },
    }


def source_identity() -> dict[str, object]:
    return {
        "git_commit": "a" * 40,
        "git_dirty": False,
        "critical_source_dirty": False,
        "dirty_state_fingerprint": "b" * 64,
        "source_tree_fingerprint": "c" * 64,
    }


def runtime_identity() -> dict[str, object]:
    return {
        "python": "3.12.0",
        "pytorch": "2.4.0",
        "transformers": "4.45.0",
        "packages": {"torch": "2.4.0", "transformers": "4.45.0"},
        "dependency_freeze_sha256": "e" * 64,
        "cuda_runtime": "12.4",
        "driver": "550.54.15",
        "model": "example/tiny-model",
        "model_revision": PINNED_REVISION,
        "dtype": "bfloat16",
        "gpu_names": ["Fake GPU"],
        "gpu_capabilities": [[8, 9]],
        "gpu_count": 1,
        "machine_id": "machine-a",
        "gce_instance_id": "instance-a",
    }


def metadata_for(config: dict[str, Any]) -> dict[str, object]:
    return create_run_metadata(config, source_identity(), runtime_identity())


def result_row(
    case: CaseSpec,
    metadata: dict[str, object],
    *,
    generation_ms: float,
    status: str = "completed",
) -> dict[str, str]:
    source = metadata["source"]
    runtime = metadata["runtime"]
    assert isinstance(source, dict)
    assert isinstance(runtime, dict)
    mib = 1024**2
    baseline_allocated = 100 * mib
    baseline_reserved = 120 * mib
    peak_allocated = 112 * mib
    peak_reserved = 140 * mib
    theoretical = case.batch_size * 2 * mib
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
            "sweep": case.sweep,
            "case_name": case.name,
            "repeat": str(case.repeat),
            "batch_size": str(case.batch_size),
            "prompt_tokens": str(case.prompt_tokens),
            "output_tokens": str(case.output_tokens),
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
            "theoretical_kv_cache_mib": str(float(theoretical / mib)),
        }
    )
    if status == "completed":
        preprocessing_ms = 2.0
        h2d_ms = 1.0
        gpu_ttft_ms = 4.0
        actual_output_tokens = case.batch_size * case.output_tokens
        row.update(
            {
                "actual_output_tokens": str(actual_output_tokens),
                "preprocessing_ms": str(preprocessing_ms),
                "h2d_ms": str(h2d_ms),
                "gpu_ttft_ms": str(gpu_ttft_ms),
                "e2e_ttft_ms": str(preprocessing_ms + h2d_ms + gpu_ttft_ms),
                "mean_tpot_ms": str(
                    (generation_ms - gpu_ttft_ms) / (case.output_tokens - 1)
                ),
                "p95_itl_ms": "2.0",
                "generation_ms": str(generation_ms),
                "e2e_latency_ms": str(
                    preprocessing_ms + h2d_ms + generation_ms
                ),
                "output_tokens_per_second": str(
                    actual_output_tokens / (generation_ms / 1_000)
                ),
                "requests_per_second": str(
                    case.batch_size / (generation_ms / 1_000)
                ),
                "output_token_hash": sha256(
                    f"{case.batch_size}:{case.prompt_tokens}:{case.output_tokens}".encode()
                ).hexdigest(),
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


def evidence(
    config: dict[str, Any],
) -> tuple[dict[str, object], list[dict[str, str]]]:
    metadata = metadata_for(config)
    generation_by_repeat = (100.0, 50.0, 25.0, 40.0, 20.0)
    rows = [
        result_row(
            case, metadata, generation_ms=generation_by_repeat[case.repeat]
        )
        for case in iter_case_specs(config)
    ]
    return metadata, rows


def resign(artifact: dict[str, object]) -> None:
    fingerprint = calibration_fingerprint(artifact)
    artifact["calibration_id"] = fingerprint
    artifact["calibration_fingerprint"] = fingerprint


def test_builds_deterministic_measured_artifact_and_selects_batch1_median() -> None:
    week2 = week2_config()
    week3 = week3_config()
    metadata, rows = evidence(week2)

    artifact = build_calibration_artifact(
        week3, week2, metadata, rows, RAW_SHA256
    )
    reordered = build_calibration_artifact(
        week3, week2, metadata, reversed(rows), RAW_SHA256
    )

    assert artifact == reordered
    assert artifact["calibration_id"] == artifact["calibration_fingerprint"]
    assert len(str(artifact["calibration_id"])) == 64
    expected_capacity = 1 / (43.0 / 1_000)
    assert artifact["capacity_rps"] == pytest.approx(expected_capacity)
    assert validate_calibration_artifact(week3, artifact) == pytest.approx(
        expected_capacity
    )
    assert [item["batch_size"] for item in artifact["measurements"]] == [1, 2, 4, 8]
    batch_one = artifact["measurements"][0]
    assert batch_one["median_e2e_service_ms"] == pytest.approx(43.0)
    assert batch_one["median_e2e_requests_per_second"] == pytest.approx(
        expected_capacity
    )
    assert artifact["selection"]["source_metric"] == (
        "batch_size_over_e2e_latency_ms"
    )
    # Week 2's requests_per_second is generation-only (25 here); it must not
    # become the online worker capacity, whose clock also includes preprocessing/H2D.
    assert artifact["capacity_rps"] != statistics.median(
        float(row["requests_per_second"])
        for row in rows
        if row["sweep"] == "batch_size" and row["batch_size"] == "1"
    )
    assert artifact["week2_identity"]["raw_csv_sha256"] == RAW_SHA256
    json.dumps(artifact, allow_nan=False)


def test_build_rejects_wrong_batch_sweep_shape_and_mixed_batch1() -> None:
    wrong_week2 = week2_config(prompt_tokens=128)
    metadata, rows = evidence(wrong_week2)
    with pytest.raises(ValueError, match="fixed batch sweep workload"):
        build_calibration_artifact(
            week3_config(), wrong_week2, metadata, rows, RAW_SHA256
        )

    week2 = week2_config()
    metadata, rows = evidence(week2)
    case = next(
        item
        for item in iter_case_specs(week2)
        if item.sweep == "batch_size"
        and item.batch_size == 1
        and item.repeat == 2
    )
    row_index = next(
        index
        for index, row in enumerate(rows)
        if row["sweep"] == "batch_size"
        and row["batch_size"] == "1"
        and row["repeat"] == "2"
    )
    rows[row_index] = result_row(
        case, metadata, generation_ms=25.0, status="oom"
    )
    with pytest.raises(ValueError, match="mixes completed and OOM"):
        build_calibration_artifact(
            week3_config(), week2, metadata, rows, RAW_SHA256
        )


def test_validator_rejects_resigned_wrong_shape_mixed_batch1_and_manual_capacity() -> None:
    week2 = week2_config()
    week3 = week3_config()
    metadata, rows = evidence(week2)
    artifact = build_calibration_artifact(week3, week2, metadata, rows, RAW_SHA256)

    wrong_shape = deepcopy(artifact)
    wrong_shape["workload"]["prompt_tokens"] = 128
    resign(wrong_shape)
    with pytest.raises(ValueError, match="wrong workload shape"):
        validate_calibration_artifact(week3, wrong_shape)

    mixed = deepcopy(artifact)
    measurement = mixed["measurements"][0]
    measurement["status_counts"] = {"completed": 4, "oom": 1, "error": 0}
    measurement["completed_repeats"] = measurement["completed_repeats"][:4]
    measurement["median_e2e_service_ms"] = 48.0
    measurement["median_e2e_requests_per_second"] = statistics.median(
        sample["e2e_requests_per_second"]
        for sample in measurement["completed_repeats"]
    )
    mixed["capacity_rps"] = measurement["median_e2e_requests_per_second"]
    resign(mixed)
    with pytest.raises(ValueError, match="mixed or failed"):
        validate_calibration_artifact(week3, mixed)

    manual = deepcopy(artifact)
    manual["capacity_rps"] = 26.0
    resign(manual)
    with pytest.raises(ValueError, match="manual override"):
        validate_calibration_artifact(week3, manual)

    fabricated_rate = deepcopy(artifact)
    fabricated_rate["measurements"][0]["completed_repeats"][0][
        "e2e_requests_per_second"
    ] = 999.0
    resign(fabricated_rate)
    with pytest.raises(ValueError, match="inconsistent E2E request rate"):
        validate_calibration_artifact(week3, fabricated_rate)


def test_validator_rejects_placeholders_and_invalid_fingerprints() -> None:
    week2 = week2_config()
    week3 = week3_config()
    metadata, rows = evidence(week2)
    artifact = build_calibration_artifact(week3, week2, metadata, rows, RAW_SHA256)

    placeholder = deepcopy(artifact)
    placeholder["status"] = "pending"
    resign(placeholder)
    with pytest.raises(ValueError, match="placeholder"):
        validate_calibration_artifact(week3, placeholder)

    invalid = deepcopy(artifact)
    invalid["calibration_fingerprint"] = "0" * 64
    with pytest.raises(ValueError, match="fingerprint is invalid"):
        validate_calibration_artifact(week3, invalid)

    for field, replacement in (
        ("run_id", "not-a-uuid"),
        ("config_fingerprint", "0" * 16),
        ("runtime_fingerprint", "0" * 16),
        ("raw_csv_sha256", "not-a-sha"),
    ):
        changed = deepcopy(artifact)
        changed["week2_identity"][field] = replacement
        resign(changed)
        with pytest.raises(ValueError):
            validate_calibration_artifact(week3, changed)

    changed_source = deepcopy(artifact)
    changed_source["week2_identity"]["source"]["git_commit"] = "not-a-commit"
    resign(changed_source)
    with pytest.raises(ValueError, match="git_commit"):
        validate_calibration_artifact(week3, changed_source)

    changed_model = deepcopy(artifact)
    changed_model["week2_identity"]["model"]["id"] = "example/other"
    resign(changed_model)
    with pytest.raises(ValueError, match="model identities"):
        validate_calibration_artifact(week3, changed_model)

    placeholder_config = week3_config("TODO_FROM_EVIDENCE.json")
    with pytest.raises(ValueError, match="placeholder"):
        validate_calibration_artifact(placeholder_config, artifact)

    manual_config = week3_config()
    manual_config["calibration"]["capacity_rps"] = 25.0
    with pytest.raises(ValueError, match="hand-entered capacity"):
        validate_calibration_artifact(manual_config, artifact)


def test_loader_optionally_validates(tmp_path: Path) -> None:
    week2 = week2_config()
    week3 = week3_config()
    metadata, rows = evidence(week2)
    artifact = build_calibration_artifact(week3, week2, metadata, rows, RAW_SHA256)
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")

    assert load_calibration_artifact(path) == artifact
    assert load_calibration_artifact(path, week3)["capacity_rps"] == pytest.approx(
        1 / (43.0 / 1_000)
    )
    assert (
        load_calibration_artifact(path, week3, runtime=runtime_identity())
        == artifact
    )
    relocated_runtime = runtime_identity()
    relocated_runtime["machine_id"] = "machine-b"
    relocated_runtime["gce_instance_id"] = "instance-b"
    assert load_calibration_artifact(path, week3, runtime=relocated_runtime) == artifact

    artifact["capacity_rps"] = 99.0
    path.write_text(json.dumps(artifact), encoding="utf-8")
    assert load_calibration_artifact(path)["capacity_rps"] == 99.0
    with pytest.raises(ValueError, match="manual override"):
        load_calibration_artifact(path, week3)


@pytest.mark.parametrize(
    ("mutation", "expected_field"),
    (
        ({"gpu_names": ["Different GPU"]}, "gpu_names"),
        ({"driver": "different-driver"}, "driver"),
        ({"packages": {"torch": "9.9", "transformers": "4.45.0"}}, "packages"),
        ({"dependency_freeze_sha256": "f" * 64}, "dependency_freeze_sha256"),
        ({"model": "example/different-model"}, "model"),
    ),
)
def test_runtime_compatibility_rejects_gpu_software_dependency_and_model_changes(
    tmp_path: Path, mutation: dict[str, object], expected_field: str
) -> None:
    week2 = week2_config()
    week3 = week3_config()
    metadata, rows = evidence(week2)
    artifact = build_calibration_artifact(week3, week2, metadata, rows, RAW_SHA256)
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    changed_runtime = {**runtime_identity(), **mutation}

    with pytest.raises(ValueError, match=expected_field):
        load_calibration_artifact(path, week3, runtime=changed_runtime)


def test_runtime_compatibility_rejects_missing_or_extra_fields(tmp_path: Path) -> None:
    week2 = week2_config()
    week3 = week3_config()
    metadata, rows = evidence(week2)
    artifact = build_calibration_artifact(week3, week2, metadata, rows, RAW_SHA256)
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")

    missing = runtime_identity()
    missing.pop("driver")
    with pytest.raises(ValueError, match=r"missing=\['driver'\]"):
        load_calibration_artifact(path, week3, runtime=missing)

    extra = runtime_identity()
    extra["new_compatibility_dimension"] = "value"
    with pytest.raises(ValueError, match="new_compatibility_dimension"):
        load_calibration_artifact(path, week3, runtime=extra)


def test_builder_requires_official_canonical_week2_evidence() -> None:
    noncanonical = week2_config()
    noncanonical["benchmark"]["warmup_runs"] = 1
    metadata, rows = evidence(noncanonical)

    with pytest.raises(ValueError, match="exactly 2 warmup runs"):
        build_calibration_artifact(
            week3_config(), noncanonical, metadata, rows, RAW_SHA256
        )


def test_validator_rejects_false_embedded_cache_claim() -> None:
    week2 = week2_config()
    week3 = week3_config()
    metadata, rows = evidence(week2)
    artifact = build_calibration_artifact(week3, week2, metadata, rows, RAW_SHA256)
    scientific = artifact["week2_identity"]["scientific_config"]
    scientific["benchmark"]["use_cache"] = False
    artifact["week2_identity"]["config_fingerprint"] = stable_fingerprint(scientific)
    resign(artifact)

    with pytest.raises(ValueError, match="KV cache enabled"):
        validate_calibration_artifact(week3, artifact)


def test_higher_batch_capacity_boundary_records_null_completed_medians() -> None:
    week2 = week2_config()
    week3 = week3_config()
    week3["calibration"]["static_batch_sizes"] = [1, 2, 4, 8, 16]
    metadata, rows = evidence(week2)
    case_by_key = {case.key: case for case in iter_case_specs(week2)}
    for index, row in enumerate(rows):
        if row["sweep"] == "batch_size" and row["batch_size"] in {"8", "16"}:
            case = case_by_key[(
                row["sweep"],
                int(row["batch_size"]),
                int(row["prompt_tokens"]),
                int(row["output_tokens"]),
                int(row["repeat"]),
            )]
            rows[index] = result_row(
                case, metadata, generation_ms=100.0, status="oom"
            )

    artifact = build_calibration_artifact(week3, week2, metadata, rows, RAW_SHA256)

    assert [item["batch_size"] for item in artifact["measurements"]] == [
        1,
        2,
        4,
        8,
        16,
    ]
    for measurement in artifact["measurements"][-2:]:
        assert measurement["status_counts"] == {
            "completed": 0,
            "oom": 5,
            "error": 0,
        }
        assert measurement["completed_repeats"] == []
        assert measurement["median_e2e_service_ms"] is None
        assert measurement["median_e2e_requests_per_second"] is None
    assert validate_calibration_artifact(week3, artifact) == pytest.approx(
        1 / (43.0 / 1_000)
    )


def test_file_generator_reads_week2_evidence_and_writes_atomically(tmp_path: Path) -> None:
    metadata_path = tmp_path / "week02-metadata.json"
    raw_path = tmp_path / "week02.csv"
    artifact_path = tmp_path / "week03-calibration.json"
    week2_path = tmp_path / "week02.json"
    week3_path = tmp_path / "week03.json"

    week2 = week2_config()
    week2["output"] = {
        "run_metadata": str(metadata_path),
        "raw_csv": str(raw_path),
    }
    week3 = week3_config(str(artifact_path))
    metadata, rows = evidence(week2)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with raw_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    # JSON is valid YAML, keeping this CPU-only test independent of YAML formatting.
    week2_path.write_text(json.dumps(week2), encoding="utf-8")
    week3_path.write_text(json.dumps(week3), encoding="utf-8")

    artifact = generate_calibration_artifact_file(week3_path, week2_path)

    assert artifact_path.is_file()
    assert load_calibration_artifact(artifact_path, week3) == artifact
    assert artifact["week2_identity"]["raw_csv_sha256"] == sha256(
        raw_path.read_bytes()
    ).hexdigest()
