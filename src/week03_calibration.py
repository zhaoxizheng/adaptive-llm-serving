from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import uuid
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from src.common import load_yaml, read_json, stable_fingerprint, write_json
from src.result_store import read_rows
from src.week01_contract import runtime_fingerprint
from src.week02_contract import (
    RESULT_FIELDS,
    validate_canonical_matrix,
    validate_official_completion,
    validate_result_rows,
    validate_run_metadata,
)

CALIBRATION_SCHEMA_VERSION = 1
CALIBRATION_ARTIFACT_TYPE = "week03_measured_capacity_calibration"
CALIBRATION_METHOD = "batch1_median_completed_requests_per_second"
PROMPT_TOKENS = 256
OUTPUT_TOKENS = 64
FULL_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
SHORT_FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{16}")
GIT_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
PLACEHOLDER_MARKERS = ("placeholder", "todo", "replace_me", "from_evidence", "pending")
NON_COMPATIBILITY_RUNTIME_FIELDS = frozenset({"machine_id", "gce_instance_id"})

_TOP_LEVEL_FIELDS = {
    "schema_version",
    "artifact_type",
    "status",
    "calibration_id",
    "calibration_fingerprint",
    "week2_identity",
    "workload",
    "static_batch_sizes",
    "measurements",
    "method",
    "selection",
    "capacity_rps",
}
_MEASUREMENT_FIELDS = {
    "batch_size",
    "terminal_repeat_count",
    "status_counts",
    "completed_repeats",
    "median_e2e_service_ms",
    "median_e2e_requests_per_second",
}
_SAMPLE_FIELDS = {"repeat", "e2e_service_ms", "e2e_requests_per_second"}
_STATUS_KEYS = {"completed", "oom", "error"}


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _exact_fields(value: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        missing = sorted(expected.difference(value))
        extra = sorted(set(value).difference(expected))
        raise ValueError(f"{name} schema mismatch; missing={missing}, extra={extra}")


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite and positive")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be finite and positive") from error
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return number


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _json_clone(value: object, name: str) -> Any:
    try:
        serialized = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return json.loads(serialized)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be JSON-serializable") from error


def _canonical_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("Calibration artifact must be canonical JSON data") from error
    return sha256(encoded).hexdigest()


def _require_sha256(value: object, name: str) -> str:
    text = str(value)
    if not FULL_SHA256_PATTERN.fullmatch(text):
        raise ValueError(f"{name} must be a lowercase full SHA-256 digest")
    return text


def _require_non_placeholder_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    lowered = value.lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        raise ValueError(f"{name} still contains a placeholder")
    return value


def _calibration_settings(
    week3_config: Mapping[str, object],
) -> tuple[str, tuple[int, ...]]:
    calibration = _mapping(week3_config.get("calibration"), "calibration")
    manual_capacity_keys = [
        str(key) for key in calibration if "capacity" in str(key).lower()
    ]
    if manual_capacity_keys:
        raise ValueError(
            "Week 3 calibration must not contain a hand-entered capacity; "
            f"remove {sorted(manual_capacity_keys)}"
        )
    missing = {"artifact", "method", "static_batch_sizes"}.difference(calibration)
    if missing:
        raise ValueError(
            f"Week 3 calibration is missing required fields: {sorted(missing)}"
        )
    artifact_path = _require_non_placeholder_text(
        calibration["artifact"], "calibration.artifact"
    )
    if calibration["method"] != CALIBRATION_METHOD:
        raise ValueError(
            f"calibration.method must be {CALIBRATION_METHOD!r}"
        )
    raw_sizes = calibration["static_batch_sizes"]
    if not isinstance(raw_sizes, list) or not raw_sizes:
        raise ValueError("calibration.static_batch_sizes must be a non-empty list")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in raw_sizes):
        raise ValueError("calibration.static_batch_sizes must contain integers")
    sizes = tuple(raw_sizes)
    if any(value <= 0 for value in sizes):
        raise ValueError("calibration.static_batch_sizes must contain positive integers")
    if tuple(sorted(set(sizes))) != sizes:
        raise ValueError(
            "calibration.static_batch_sizes must be unique and strictly increasing"
        )
    if 1 not in sizes:
        raise ValueError(
            "calibration.static_batch_sizes must include batch size 1 for capacity selection"
        )

    workload = _mapping(week3_config.get("workload"), "workload")
    if (workload.get("prompt_tokens"), workload.get("output_tokens")) != (
        PROMPT_TOKENS,
        OUTPUT_TOKENS,
    ):
        raise ValueError(
            f"Week 3 calibration requires workload shape {PROMPT_TOKENS}/{OUTPUT_TOKENS}"
        )
    return artifact_path, sizes


def calibration_artifact_path(week3_config: Mapping[str, object]) -> Path:
    path, _ = _calibration_settings(week3_config)
    return Path(path)


def _model_identity(
    week2_config: Mapping[str, object], metadata: Mapping[str, object]
) -> dict[str, str]:
    configured = _mapping(week2_config.get("model"), "Week 2 model")
    runtime = _mapping(metadata.get("runtime"), "Week 2 metadata.runtime")
    identity = {
        "id": str(runtime.get("model", "")),
        "revision": str(runtime.get("model_revision", "")),
        "dtype": str(runtime.get("dtype", "")),
    }
    configured_identity = {
        "id": str(configured.get("id", "")),
        "revision": str(configured.get("revision", "")),
        "dtype": str(configured.get("dtype", "")),
    }
    if identity != configured_identity:
        raise ValueError("Week 2 runtime model identity differs from its config")
    return identity


def _require_matching_week3_model(
    week3_config: Mapping[str, object], model_identity: Mapping[str, object]
) -> None:
    model = _mapping(week3_config.get("model"), "Week 3 model")
    expected = {
        "id": str(model.get("id", "")),
        "revision": str(model.get("revision", "")),
        "dtype": str(model.get("dtype", "")),
    }
    if dict(model_identity) != expected:
        raise ValueError("Week 3 model identity differs from the Week 2 calibration run")


def _selected_rows(
    rows: Sequence[Mapping[str, str]], sizes: Sequence[int]
) -> dict[int, list[Mapping[str, str]]]:
    batch_sweep = [row for row in rows if row["sweep"] == "batch_size"]
    if not batch_sweep:
        raise ValueError("Week 2 results do not contain the batch_size sweep")
    wrong_shape = [
        row["case_name"]
        for row in batch_sweep
        if (int(row["prompt_tokens"]), int(row["output_tokens"]))
        != (PROMPT_TOKENS, OUTPUT_TOKENS)
    ]
    if wrong_shape:
        raise ValueError(
            "Week 2 batch_size sweep contains a non-calibration workload shape: "
            f"{wrong_shape[:3]}"
        )
    selected: dict[int, list[Mapping[str, str]]] = {size: [] for size in sizes}
    for row in batch_sweep:
        batch_size = int(row["batch_size"])
        if batch_size in selected:
            selected[batch_size].append(row)
    missing = [size for size, group in selected.items() if not group]
    if missing:
        raise ValueError(
            f"Week 2 batch_size sweep is missing configured static batch sizes: {missing}"
        )
    return selected


def _measurement(
    batch_size: int, rows: Sequence[Mapping[str, str]]
) -> dict[str, object]:
    ordered = sorted(rows, key=lambda row: int(row["repeat"]))
    status_counts = {
        status: sum(row["status"] == status for row in ordered)
        for status in ("completed", "oom", "error")
    }
    completed = [row for row in ordered if row["status"] == "completed"]
    if batch_size == 1 and not completed:
        raise ValueError(
            "Batch-size-1 calibration failed: it has no completed repeats"
        )
    if batch_size == 1 and status_counts != {
        "completed": len(ordered),
        "oom": 0,
        "error": 0,
    }:
        raise ValueError(
            "Batch-size-1 calibration must have only completed repeats; mixed or failed "
            "batch-size-1 evidence cannot select capacity"
        )
    samples: list[dict[str, int | float]] = []
    for row in completed:
        # Week 3's worker clock includes preprocessing, H2D, and generation.
        # Recompute rate from Week 2's validated E2E latency instead of copying
        # its generation-only requests_per_second column.
        e2e_service_ms = float(row["e2e_latency_ms"])
        samples.append(
            {
                "repeat": int(row["repeat"]),
                "e2e_service_ms": e2e_service_ms,
                "e2e_requests_per_second": batch_size / (e2e_service_ms / 1_000),
            }
        )
    median_service = (
        statistics.median(sample["e2e_service_ms"] for sample in samples)
        if samples
        else None
    )
    median_rate = (
        statistics.median(
            sample["e2e_requests_per_second"] for sample in samples
        )
        if samples
        else None
    )
    return {
        "batch_size": batch_size,
        "terminal_repeat_count": len(ordered),
        "status_counts": status_counts,
        "completed_repeats": samples,
        "median_e2e_service_ms": median_service,
        "median_e2e_requests_per_second": median_rate,
    }


def calibration_fingerprint(artifact: Mapping[str, object]) -> str:
    """Return the full deterministic digest, excluding the two self-identifiers."""

    payload = dict(artifact)
    payload.pop("calibration_id", None)
    payload.pop("calibration_fingerprint", None)
    return _canonical_sha256(payload)


def build_calibration_artifact(
    config: Mapping[str, object],
    week2_config: Mapping[str, object],
    metadata: Mapping[str, object],
    rows: Iterable[Mapping[str, str]],
    raw_sha256: str,
) -> dict[str, object]:
    """Build a deterministic Week 3 capacity artifact from validated Week 2 rows.

    ``config`` is the Week 3 config. The complete Week 2 result set is validated
    before the exact 256-prompt/64-output ``batch_size`` slice is aggregated.
    """

    _, sizes = _calibration_settings(config)
    raw_digest = _require_sha256(raw_sha256, "raw_sha256")
    validate_canonical_matrix(week2_config)
    validate_run_metadata(metadata, week2_config)
    materialized = [dict(row) for row in rows]
    for line, row in enumerate(materialized, start=2):
        if set(row) != set(RESULT_FIELDS):
            missing = sorted(set(RESULT_FIELDS).difference(row))
            extra = sorted(set(row).difference(RESULT_FIELDS))
            raise ValueError(
                f"Week 2 row {line} schema mismatch; missing={missing}, extra={extra}"
            )
    validate_result_rows(
        materialized, metadata, week2_config, require_complete=True
    )
    validate_official_completion(materialized, week2_config)

    model_identity = _model_identity(week2_config, metadata)
    _require_matching_week3_model(config, model_identity)
    selected = _selected_rows(materialized, sizes)
    measurements = [_measurement(size, selected[size]) for size in sizes]
    batch_one = next(item for item in measurements if item["batch_size"] == 1)
    capacity_rps = float(batch_one["median_e2e_requests_per_second"])

    source = _mapping(metadata.get("source"), "Week 2 metadata.source")
    runtime = _mapping(metadata.get("runtime"), "Week 2 metadata.runtime")
    scientific_config = _mapping(
        metadata.get("scientific_config"), "Week 2 metadata.scientific_config"
    )
    core: dict[str, object] = {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "artifact_type": CALIBRATION_ARTIFACT_TYPE,
        "status": "completed",
        "week2_identity": {
            "run_id": str(metadata["run_id"]),
            "config_fingerprint": str(metadata["config_fingerprint"]),
            "runtime_fingerprint": str(metadata["runtime_fingerprint"]),
            "raw_csv_sha256": raw_digest,
            "source": _json_clone(source, "Week 2 source identity"),
            "runtime": _json_clone(runtime, "Week 2 runtime identity"),
            "scientific_config": _json_clone(
                scientific_config, "Week 2 scientific config"
            ),
            "model": model_identity,
        },
        "workload": {
            "sweep": "batch_size",
            "prompt_tokens": PROMPT_TOKENS,
            "output_tokens": OUTPUT_TOKENS,
            "use_cache": True,
        },
        "static_batch_sizes": list(sizes),
        "measurements": measurements,
        "method": CALIBRATION_METHOD,
        "selection": {
            "method": CALIBRATION_METHOD,
            "batch_size": 1,
            "source_metric": "batch_size_over_e2e_latency_ms",
            "aggregation": "median_over_completed_repeats",
        },
        "capacity_rps": capacity_rps,
    }
    fingerprint = _canonical_sha256(core)
    artifact = {
        **core,
        "calibration_id": fingerprint,
        "calibration_fingerprint": fingerprint,
    }
    artifact = _json_clone(artifact, "calibration artifact")
    validate_calibration_artifact(config, artifact)
    return artifact


def _validate_week2_identity(
    week3_config: Mapping[str, object], value: object
) -> None:
    identity = _mapping(value, "artifact.week2_identity")
    expected_fields = {
        "run_id",
        "config_fingerprint",
        "runtime_fingerprint",
        "raw_csv_sha256",
        "source",
        "runtime",
        "scientific_config",
        "model",
    }
    _exact_fields(identity, expected_fields, "artifact.week2_identity")
    try:
        uuid.UUID(str(identity["run_id"]))
    except ValueError as error:
        raise ValueError("artifact Week 2 run_id is invalid") from error
    if not SHORT_FINGERPRINT_PATTERN.fullmatch(str(identity["config_fingerprint"])):
        raise ValueError("artifact Week 2 config_fingerprint is invalid")
    if not SHORT_FINGERPRINT_PATTERN.fullmatch(str(identity["runtime_fingerprint"])):
        raise ValueError("artifact Week 2 runtime_fingerprint is invalid")
    _require_sha256(identity["raw_csv_sha256"], "artifact Week 2 raw_csv_sha256")

    source = _mapping(identity["source"], "artifact Week 2 source")
    required_source = {
        "git_commit",
        "git_dirty",
        "dirty_state_fingerprint",
        "critical_source_dirty",
        "source_tree_fingerprint",
    }
    _exact_fields(source, required_source, "artifact Week 2 source")
    if not GIT_COMMIT_PATTERN.fullmatch(str(source["git_commit"])):
        raise ValueError("artifact Week 2 git_commit is invalid")
    if not FULL_SHA256_PATTERN.fullmatch(str(source["dirty_state_fingerprint"])):
        raise ValueError("artifact Week 2 dirty_state_fingerprint is invalid")
    if not FULL_SHA256_PATTERN.fullmatch(str(source["source_tree_fingerprint"])):
        raise ValueError("artifact Week 2 source_tree_fingerprint is invalid")
    if not isinstance(source["git_dirty"], bool) or not isinstance(
        source["critical_source_dirty"], bool
    ):
        raise ValueError("artifact Week 2 source dirty flags must be booleans")

    runtime = _mapping(identity["runtime"], "artifact Week 2 runtime")
    if runtime_fingerprint(runtime) != identity["runtime_fingerprint"]:
        raise ValueError("artifact Week 2 runtime_fingerprint is inconsistent")
    scientific = _mapping(
        identity["scientific_config"], "artifact Week 2 scientific_config"
    )
    if stable_fingerprint(scientific) != identity["config_fingerprint"]:
        raise ValueError("artifact Week 2 config_fingerprint is inconsistent")
    validate_canonical_matrix(scientific)

    model = _mapping(identity["model"], "artifact Week 2 model")
    _exact_fields(model, {"id", "revision", "dtype"}, "artifact Week 2 model")
    expected_model = {
        "id": str(runtime.get("model", "")),
        "revision": str(runtime.get("model_revision", "")),
        "dtype": str(runtime.get("dtype", "")),
    }
    scientific_model = _mapping(
        scientific.get("model"), "artifact Week 2 scientific_config.model"
    )
    configured_model = {
        "id": str(scientific_model.get("id", "")),
        "revision": str(scientific_model.get("revision", "")),
        "dtype": str(scientific_model.get("dtype", "")),
    }
    if dict(model) != expected_model or dict(model) != configured_model:
        raise ValueError("artifact Week 2 model identities are inconsistent")
    if not GIT_COMMIT_PATTERN.fullmatch(str(model["revision"])):
        raise ValueError("artifact model revision is not immutable")
    for field in ("id", "dtype"):
        _require_non_placeholder_text(model[field], f"artifact model {field}")
    _require_matching_week3_model(week3_config, model)


def _same_number(actual: object, expected: float, name: str) -> None:
    number = _positive_number(actual, name)
    if not math.isclose(number, expected, rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError(f"{name} is inconsistent with completed-repeat evidence")


def runtime_compatibility_identity(runtime: Mapping[str, object]) -> dict[str, object]:
    """Return fields that affect whether a measured calibration is reusable.

    Host instance identifiers describe provenance, not the GPU/software execution
    environment. Every other recorded field is compatibility-significant, and exact
    key-set comparison prevents a newly added runtime field from being ignored.
    """

    return {
        str(key): _json_clone(value, f"runtime compatibility field {key}")
        for key, value in runtime.items()
        if key not in NON_COMPATIBILITY_RUNTIME_FIELDS
    }


def validate_runtime_compatibility(
    calibrated_runtime: Mapping[str, object], current_runtime: Mapping[str, object]
) -> None:
    calibrated = runtime_compatibility_identity(calibrated_runtime)
    current = runtime_compatibility_identity(current_runtime)
    calibrated_fields = set(calibrated)
    current_fields = set(current)
    if calibrated_fields != current_fields:
        raise ValueError(
            "Current Week 3 runtime compatibility fields differ from the Week 2 "
            "calibration runtime; "
            f"missing={sorted(calibrated_fields - current_fields)}, "
            f"extra={sorted(current_fields - calibrated_fields)}"
        )
    changed = sorted(
        field for field in calibrated_fields if calibrated[field] != current[field]
    )
    if changed:
        raise ValueError(
            "Current Week 3 runtime differs from the Week 2 calibration runtime "
            f"for fields: {changed}"
        )


def validate_calibration_artifact(
    week3_config: Mapping[str, object], artifact: Mapping[str, object]
) -> float:
    """Validate a measured artifact and return its selected batch-1 capacity."""

    _, sizes = _calibration_settings(week3_config)
    if not isinstance(artifact, Mapping):
        raise ValueError("Calibration artifact must be a mapping")
    _exact_fields(artifact, _TOP_LEVEL_FIELDS, "calibration artifact")
    if artifact["schema_version"] != CALIBRATION_SCHEMA_VERSION:
        raise ValueError("Unsupported Week 3 calibration schema_version")
    if artifact["artifact_type"] != CALIBRATION_ARTIFACT_TYPE:
        raise ValueError("Calibration artifact_type is invalid")
    if artifact["status"] != "completed":
        raise ValueError("Calibration artifact is a placeholder or did not complete")
    if artifact["static_batch_sizes"] != list(sizes):
        raise ValueError("Calibration artifact static_batch_sizes differ from Week 3 config")
    if artifact["method"] != CALIBRATION_METHOD:
        raise ValueError("Calibration artifact method is invalid")
    expected_workload = {
        "sweep": "batch_size",
        "prompt_tokens": PROMPT_TOKENS,
        "output_tokens": OUTPUT_TOKENS,
        "use_cache": True,
    }
    if artifact["workload"] != expected_workload:
        raise ValueError("Calibration artifact has the wrong workload shape")

    _validate_week2_identity(week3_config, artifact["week2_identity"])
    identity = _mapping(artifact["week2_identity"], "artifact.week2_identity")
    scientific = _mapping(identity["scientific_config"], "scientific_config")
    benchmark = _mapping(scientific.get("benchmark"), "scientific_config.benchmark")
    repeats = _nonnegative_integer(benchmark.get("repeats"), "Week 2 repeats")
    if repeats < 1:
        raise ValueError("Week 2 repeats must be positive")
    if benchmark.get("use_cache") is not True:
        raise ValueError("Calibration artifact Week 2 evidence must have KV cache enabled")

    selection = _mapping(artifact["selection"], "artifact.selection")
    expected_selection = {
        "method": CALIBRATION_METHOD,
        "batch_size": 1,
        "source_metric": "batch_size_over_e2e_latency_ms",
        "aggregation": "median_over_completed_repeats",
    }
    if dict(selection) != expected_selection:
        raise ValueError("Calibration selection method is invalid or incomplete")

    measurements = artifact["measurements"]
    if not isinstance(measurements, list) or len(measurements) != len(sizes):
        raise ValueError("Calibration measurements do not cover static_batch_sizes")
    medians: dict[int, float] = {}
    observed_sizes: list[int] = []
    for index, raw_measurement in enumerate(measurements):
        measurement = _mapping(raw_measurement, f"measurement {index}")
        _exact_fields(measurement, _MEASUREMENT_FIELDS, f"measurement {index}")
        batch_size = _nonnegative_integer(
            measurement["batch_size"], f"measurement {index} batch_size"
        )
        observed_sizes.append(batch_size)
        terminal_count = _nonnegative_integer(
            measurement["terminal_repeat_count"],
            f"batch {batch_size} terminal_repeat_count",
        )
        if terminal_count != repeats:
            raise ValueError(
                f"batch {batch_size} terminal repeats differ from Week 2 config"
            )
        status_counts = _mapping(
            measurement["status_counts"], f"batch {batch_size} status_counts"
        )
        _exact_fields(status_counts, _STATUS_KEYS, f"batch {batch_size} status_counts")
        counts = {
            status: _nonnegative_integer(
                status_counts[status], f"batch {batch_size} {status} count"
            )
            for status in _STATUS_KEYS
        }
        if sum(counts.values()) != terminal_count:
            raise ValueError(f"batch {batch_size} status counts are inconsistent")

        raw_samples = measurement["completed_repeats"]
        if not isinstance(raw_samples, list) or len(raw_samples) != counts["completed"]:
            raise ValueError(f"batch {batch_size} completed-repeat count is inconsistent")
        samples: list[tuple[int, float, float]] = []
        for sample_index, raw_sample in enumerate(raw_samples):
            sample = _mapping(
                raw_sample, f"batch {batch_size} completed repeat {sample_index}"
            )
            _exact_fields(
                sample,
                _SAMPLE_FIELDS,
                f"batch {batch_size} completed repeat {sample_index}",
            )
            repeat = _nonnegative_integer(
                sample["repeat"], f"batch {batch_size} repeat"
            )
            if repeat >= repeats:
                raise ValueError(f"batch {batch_size} repeat is outside the Week 2 matrix")
            service_ms = _positive_number(
                sample["e2e_service_ms"],
                f"batch {batch_size} e2e_service_ms",
            )
            request_rate = _positive_number(
                sample["e2e_requests_per_second"],
                f"batch {batch_size} e2e_requests_per_second",
            )
            expected_rate = batch_size / (service_ms / 1_000)
            if not math.isclose(
                request_rate, expected_rate, rel_tol=1e-12, abs_tol=1e-12
            ):
                raise ValueError(
                    f"batch {batch_size} completed repeat {repeat} has an "
                    "inconsistent E2E request rate"
                )
            samples.append((repeat, service_ms, request_rate))
        repeat_indices = [sample[0] for sample in samples]
        if repeat_indices != sorted(set(repeat_indices)):
            raise ValueError(f"batch {batch_size} completed repeats are duplicate or unordered")
        if samples:
            median_service = statistics.median(sample[1] for sample in samples)
            median_rate = statistics.median(sample[2] for sample in samples)
            _same_number(
                measurement["median_e2e_service_ms"],
                median_service,
                f"batch {batch_size} median_e2e_service_ms",
            )
            _same_number(
                measurement["median_e2e_requests_per_second"],
                median_rate,
                f"batch {batch_size} median_e2e_requests_per_second",
            )
            medians[batch_size] = median_rate
        elif (
            measurement["median_e2e_service_ms"] is not None
            or measurement["median_e2e_requests_per_second"] is not None
        ):
            raise ValueError(
                f"batch {batch_size} without completed repeats must have null medians"
            )
        if batch_size == 1 and counts != {
            "completed": repeats,
            "oom": 0,
            "error": 0,
        }:
            raise ValueError(
                "Batch-size-1 calibration is mixed or failed; every repeat must complete"
            )

    if observed_sizes != list(sizes):
        raise ValueError("Calibration measurements are duplicate, missing, or out of order")
    capacity = _positive_number(artifact["capacity_rps"], "artifact.capacity_rps")
    if not math.isclose(capacity, medians[1], rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError(
            "artifact.capacity_rps is a manual override; it must equal the measured "
            "batch-size-1 completed-repeat median"
        )

    calibration_id = _require_sha256(artifact["calibration_id"], "calibration_id")
    fingerprint = _require_sha256(
        artifact["calibration_fingerprint"], "calibration_fingerprint"
    )
    expected_fingerprint = calibration_fingerprint(artifact)
    if calibration_id != fingerprint or fingerprint != expected_fingerprint:
        raise ValueError("Calibration ID or fingerprint is invalid")
    return capacity


def load_calibration_artifact(
    path: str | Path,
    week3_config: Mapping[str, object] | None = None,
    *,
    runtime: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Load an artifact and optionally validate it against a Week 3 config."""

    payload = read_json(path)
    if not isinstance(payload, Mapping):
        raise ValueError("Calibration artifact file must contain a JSON object")
    artifact = dict(payload)
    if week3_config is not None:
        validate_calibration_artifact(week3_config, artifact)
    if runtime is not None:
        identity = _mapping(artifact.get("week2_identity"), "artifact.week2_identity")
        calibrated_runtime = _mapping(
            identity.get("runtime"), "artifact Week 2 runtime"
        )
        validate_runtime_compatibility(calibrated_runtime, runtime)
    return artifact


def load_configured_calibration_artifact(
    week3_config: Mapping[str, object],
    *,
    runtime: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return load_calibration_artifact(
        calibration_artifact_path(week3_config), week3_config, runtime=runtime
    )


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate_calibration_artifact_file(
    week3_config_path: str | Path, week2_config_path: str | Path
) -> dict[str, object]:
    week3_config = load_yaml(week3_config_path)
    week2_config = load_yaml(week2_config_path)
    output = _mapping(week2_config.get("output"), "Week 2 output")
    metadata_path = Path(str(output["run_metadata"]))
    raw_path = Path(str(output["raw_csv"]))
    if not metadata_path.is_file():
        raise ValueError(f"Missing Week 2 run metadata: {metadata_path}")
    if not raw_path.is_file() or raw_path.stat().st_size == 0:
        raise ValueError(f"Missing or empty Week 2 raw CSV: {raw_path}")
    metadata = read_json(metadata_path)
    if not isinstance(metadata, Mapping):
        raise ValueError("Week 2 run metadata must contain a JSON object")
    rows = read_rows(raw_path, expected_fields=RESULT_FIELDS)
    artifact = build_calibration_artifact(
        week3_config,
        week2_config,
        metadata,
        rows,
        _sha256_file(raw_path),
    )
    destination = calibration_artifact_path(week3_config)
    write_json(destination, artifact)
    return artifact


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build measured Week 3 capacity calibration from Week 2 evidence."
    )
    parser.add_argument("--week3-config", default="configs/week03.yaml")
    parser.add_argument("--week2-config", default="configs/week02.yaml")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        artifact = generate_calibration_artifact_file(
            args.week3_config, args.week2_config
        )
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(f"Week 3 calibration failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(
        "Week 3 calibration ready: "
        f"id={artifact['calibration_id']}, capacity_rps={artifact['capacity_rps']}"
    )


if __name__ == "__main__":
    main()
