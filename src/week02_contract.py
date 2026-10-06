from __future__ import annotations

import math
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping

from src.common import stable_fingerprint, utc_now
from src.result_store import parse_bool
from src.week02_smoke import smoke_workload
from src.week01_contract import (
    collect_runtime_identity as collect_runtime_identity,
    runtime_fingerprint,
    source_contract,
    validate_model_snapshot as validate_model_snapshot,
)

SCHEMA_VERSION = 3
PINNED_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
TERMINAL_STATUSES = {"completed", "oom", "error"}

RESULT_FIELDS = [
    "timestamp",
    "run_id",
    "git_commit",
    "config_fingerprint",
    "runtime_fingerprint",
    "model",
    "model_revision",
    "dtype",
    "sweep",
    "case_name",
    "repeat",
    "batch_size",
    "prompt_tokens",
    "output_tokens",
    "use_cache",
    "status",
    "error_phase",
    "error_type",
    "error_message",
    "actual_output_tokens",
    "preprocessing_ms",
    "h2d_ms",
    "gpu_ttft_ms",
    "e2e_ttft_ms",
    "mean_tpot_ms",
    "p95_itl_ms",
    "generation_ms",
    "e2e_latency_ms",
    "output_tokens_per_second",
    "requests_per_second",
    "model_baseline_allocated_bytes",
    "model_baseline_reserved_bytes",
    "peak_memory_allocated_bytes",
    "peak_memory_reserved_bytes",
    "memory_allocated_delta_bytes",
    "memory_reserved_delta_bytes",
    "model_baseline_allocated_mb",
    "model_baseline_reserved_mb",
    "peak_memory_allocated_mb",
    "peak_memory_reserved_mb",
    "memory_allocated_delta_mb",
    "memory_reserved_delta_mb",
    "theoretical_kv_cache_bytes",
    "theoretical_kv_cache_mib",
    "output_token_hash",
]

IDENTITY_FIELDS = {
    "run_id",
    "git_commit",
    "config_fingerprint",
    "runtime_fingerprint",
    "model",
    "model_revision",
    "dtype",
}
METRIC_FIELDS = {
    "actual_output_tokens",
    "preprocessing_ms",
    "h2d_ms",
    "gpu_ttft_ms",
    "e2e_ttft_ms",
    "mean_tpot_ms",
    "p95_itl_ms",
    "generation_ms",
    "e2e_latency_ms",
    "output_tokens_per_second",
    "requests_per_second",
}
MEMORY_BYTE_FIELDS = {
    "model_baseline_allocated_bytes",
    "model_baseline_reserved_bytes",
    "peak_memory_allocated_bytes",
    "peak_memory_reserved_bytes",
    "memory_allocated_delta_bytes",
    "memory_reserved_delta_bytes",
    "theoretical_kv_cache_bytes",
}
MEMORY_MB_FIELDS = {
    "model_baseline_allocated_mb",
    "model_baseline_reserved_mb",
    "peak_memory_allocated_mb",
    "peak_memory_reserved_mb",
    "memory_allocated_delta_mb",
    "memory_reserved_delta_mb",
    "theoretical_kv_cache_mib",
}


@dataclass(frozen=True, order=True)
class CaseSpec:
    sweep: str
    batch_size: int
    prompt_tokens: int
    output_tokens: int
    repeat: int

    @property
    def name(self) -> str:
        return (
            f"{self.sweep}-b{self.batch_size:02d}-p{self.prompt_tokens:04d}-"
            f"o{self.output_tokens:03d}-r{self.repeat:02d}"
        )

    @property
    def key(self) -> tuple[str, int, int, int, int]:
        return (
            self.sweep,
            self.batch_size,
            self.prompt_tokens,
            self.output_tokens,
            self.repeat,
        )


def scientific_config(config: Mapping[str, object]) -> dict[str, object]:
    required = ("model", "generation", "benchmark")
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(f"Week 2 config is missing sections: {missing}")
    snapshot = {key: deepcopy(config[key]) for key in required}
    model = snapshot["model"]
    if not isinstance(model, dict):
        raise ValueError("model must be a mapping")
    if not PINNED_REVISION_PATTERN.fullmatch(str(model.get("revision", ""))):
        raise ValueError(
            "model.revision must be an immutable 40-character Hugging Face commit SHA"
        )
    return snapshot


def config_fingerprint(config: Mapping[str, object]) -> str:
    return stable_fingerprint(scientific_config(config))


def _integer_values(value: object, name: str) -> list[int]:
    raw_values = value if isinstance(value, list) else [value]
    values: list[int] = []
    for raw in raw_values:
        if isinstance(raw, bool) or raw is None:
            raise ValueError(f"{name} must contain positive integers")
        try:
            integer = int(raw)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} must contain positive integers") from error
        if integer < 1:
            raise ValueError(f"{name} must contain positive integers")
        values.append(integer)
    return values


def iter_case_specs(config: Mapping[str, object]) -> list[CaseSpec]:
    benchmark = config.get("benchmark")
    if not isinstance(benchmark, Mapping):
        raise ValueError("benchmark must be a mapping")
    sweeps = benchmark.get("sweeps")
    if not isinstance(sweeps, Mapping) or not sweeps:
        raise ValueError("benchmark.sweeps must be a non-empty mapping")
    repeats = int(benchmark.get("repeats", 0))
    if repeats < 1:
        raise ValueError("benchmark.repeats must be positive")
    cases: list[CaseSpec] = []
    for sweep, raw_dimensions in sweeps.items():
        if not isinstance(raw_dimensions, Mapping):
            raise ValueError(f"benchmark.sweeps.{sweep} must be a mapping")
        batches = _integer_values(raw_dimensions.get("batch_size"), "batch_size")
        prompts = _integer_values(raw_dimensions.get("prompt_tokens"), "prompt_tokens")
        outputs = _integer_values(raw_dimensions.get("output_tokens"), "output_tokens")
        for batch_size in batches:
            for prompt_tokens in prompts:
                for output_tokens in outputs:
                    for repeat in range(repeats):
                        cases.append(
                            CaseSpec(
                                str(sweep),
                                batch_size,
                                prompt_tokens,
                                output_tokens,
                                repeat,
                            )
                        )
    if len({case.key for case in cases}) != len(cases):
        raise ValueError("Week 2 config expands to duplicate cases")
    return cases


def expected_cases(config: Mapping[str, object]) -> set[tuple[str, int, int, int, int]]:
    return {case.key for case in iter_case_specs(config)}


def case_key(row: Mapping[str, object]) -> tuple[str, int, int, int, int]:
    return (
        str(row["sweep"]),
        int(row["batch_size"]),
        int(row["prompt_tokens"]),
        int(row["output_tokens"]),
        int(row["repeat"]),
    )


def validate_canonical_matrix(config: Mapping[str, object]) -> None:
    expected_dimensions = {
        "prompt_length": {(1, value, 64) for value in (32, 256, 1024, 2048)},
        "output_length": {(1, 256, value) for value in (16, 64, 256)},
        "batch_size": {(value, 256, 64) for value in (1, 2, 4, 8, 16)},
    }
    cases = iter_case_specs(config)
    benchmark = config["benchmark"]
    assert isinstance(benchmark, Mapping)
    if int(benchmark.get("warmup_runs", -1)) != 2:
        raise ValueError("The canonical Week 2 matrix requires exactly 2 warmup runs")
    if int(benchmark.get("repeats", -1)) != 5:
        raise ValueError("The canonical Week 2 matrix requires exactly 5 repeats")
    if parse_bool(benchmark.get("use_cache", False)) is not True:
        raise ValueError("The canonical Week 2 matrix requires KV cache enabled")
    smoke_sizes, smoke_prompt, smoke_output = smoke_workload(config)
    if smoke_sizes != [1, 2, 4]:
        raise ValueError(
            "The canonical Week 2 smoke_batch_sizes must be ordered exactly [1, 2, 4]"
        )
    if (smoke_prompt, smoke_output) != (256, 64):
        raise ValueError("The canonical Week 2 smoke must use the fixed batch sweep workload")
    actual_dimensions: dict[str, set[tuple[int, int, int]]] = {}
    for case in cases:
        actual_dimensions.setdefault(case.sweep, set()).add(
            (case.batch_size, case.prompt_tokens, case.output_tokens)
        )
    if actual_dimensions != expected_dimensions or len(cases) != 60:
        raise ValueError("Week 2 must expand to the exact named 60-case sweep matrix")


def create_run_metadata(
    config: Mapping[str, object],
    source: Mapping[str, object],
    runtime: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(uuid.uuid4()),
        "started_at": utc_now(),
        "source": source_contract(source),
        "config_fingerprint": config_fingerprint(config),
        "runtime_fingerprint": runtime_fingerprint(runtime),
        "scientific_config": scientific_config(config),
        "runtime": dict(runtime),
        "expected_case_count": len(expected_cases(config)),
    }


def validate_run_metadata(
    metadata: Mapping[str, object],
    config: Mapping[str, object],
    source: Mapping[str, object] | None = None,
    runtime: Mapping[str, object] | None = None,
) -> None:
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported or missing Week 2 metadata schema_version")
    try:
        uuid.UUID(str(metadata["run_id"]))
    except (KeyError, ValueError) as error:
        raise ValueError("Run metadata contains an invalid run_id") from error
    if metadata.get("config_fingerprint") != config_fingerprint(config):
        raise ValueError("Run metadata does not match the current scientific config")
    if metadata.get("scientific_config") != scientific_config(config):
        raise ValueError("Run metadata scientific_config is inconsistent")
    if metadata.get("expected_case_count") != len(expected_cases(config)):
        raise ValueError("Run metadata expected_case_count is inconsistent")
    if source is not None and metadata.get("source") != source_contract(source):
        raise ValueError("Cannot resume: Git source identity differs from the original run")
    if runtime is not None:
        if metadata.get("runtime_fingerprint") != runtime_fingerprint(runtime):
            raise ValueError("Cannot resume: GPU or software runtime identity changed")
        if metadata.get("runtime") != dict(runtime):
            raise ValueError("Run metadata runtime details are inconsistent")


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Run metadata {name} must be a mapping")
    return value


def _number(row: Mapping[str, str], field: str, line: int) -> float:
    raw = row.get(field, "")
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"Row {line} has an invalid number in {field}: {raw!r}") from error
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Row {line} has a negative or non-finite {field}")
    return value


def _close(actual: float, expected: float) -> bool:
    return math.isclose(actual, expected, rel_tol=1e-8, abs_tol=1e-6)


def _validate_memory(row: Mapping[str, str], line: int) -> None:
    byte_values = {field: int(row[field]) for field in MEMORY_BYTE_FIELDS}
    if any(value < 0 for value in byte_values.values()):
        raise ValueError(f"Row {line} contains negative memory bytes")
    mb_values = {field: _number(row, field, line) for field in MEMORY_MB_FIELDS}
    pairs = (
        ("model_baseline_allocated_bytes", "model_baseline_allocated_mb"),
        ("model_baseline_reserved_bytes", "model_baseline_reserved_mb"),
        ("peak_memory_allocated_bytes", "peak_memory_allocated_mb"),
        ("peak_memory_reserved_bytes", "peak_memory_reserved_mb"),
        ("memory_allocated_delta_bytes", "memory_allocated_delta_mb"),
        ("memory_reserved_delta_bytes", "memory_reserved_delta_mb"),
        ("theoretical_kv_cache_bytes", "theoretical_kv_cache_mib"),
    )
    for byte_field, mb_field in pairs:
        if not _close(mb_values[mb_field], byte_values[byte_field] / (1024**2)):
            raise ValueError(f"Row {line} has inconsistent {mb_field}")
    expected_allocated_delta = max(
        0,
        byte_values["peak_memory_allocated_bytes"]
        - byte_values["model_baseline_allocated_bytes"],
    )
    expected_reserved_delta = max(
        0,
        byte_values["peak_memory_reserved_bytes"]
        - byte_values["model_baseline_reserved_bytes"],
    )
    if byte_values["memory_allocated_delta_bytes"] != expected_allocated_delta:
        raise ValueError(f"Row {line} has inconsistent allocated memory delta")
    if byte_values["memory_reserved_delta_bytes"] != expected_reserved_delta:
        raise ValueError(f"Row {line} has inconsistent reserved memory delta")


def validate_result_rows(
    rows: Iterable[Mapping[str, str]],
    metadata: Mapping[str, object],
    config: Mapping[str, object],
    *,
    require_complete: bool,
) -> set[tuple[str, int, int, int, int]]:
    expected = expected_cases(config)
    specs = {case.key: case for case in iter_case_specs(config)}
    source = _mapping(metadata.get("source"), "source")
    runtime = _mapping(metadata.get("runtime"), "runtime")
    expected_identity = {
        "run_id": str(metadata["run_id"]),
        "git_commit": str(source["git_commit"]),
        "config_fingerprint": str(metadata["config_fingerprint"]),
        "runtime_fingerprint": str(metadata["runtime_fingerprint"]),
        "model": str(runtime["model"]),
        "model_revision": str(runtime["model_revision"]),
        "dtype": str(runtime["dtype"]),
    }
    seen: set[tuple[str, int, int, int, int]] = set()
    output_hashes: dict[tuple[int, int, int], str] = {}
    for line, row in enumerate(rows, start=2):
        missing_fields = set(RESULT_FIELDS).difference(row)
        if missing_fields:
            raise ValueError(f"Row {line} is missing fields: {sorted(missing_fields)}")
        for field in IDENTITY_FIELDS:
            if row[field] != expected_identity[field]:
                raise ValueError(f"Row {line} identity mismatch for {field}")
        try:
            datetime.fromisoformat(row["timestamp"])
        except ValueError as error:
            raise ValueError(f"Row {line} has an invalid timestamp") from error
        key = case_key(row)
        if key in seen:
            raise ValueError(f"Duplicate result case at row {line}: {key}")
        if key not in expected:
            raise ValueError(f"Unexpected result case at row {line}: {key}")
        seen.add(key)
        if row["case_name"] != specs[key].name:
            raise ValueError(f"Row {line} has an inconsistent case_name")
        if parse_bool(row["use_cache"]) is not True:
            raise ValueError(f"Row {line} must have KV cache enabled")
        status = row["status"]
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"Row {line} has an invalid terminal status: {status!r}")
        _validate_memory(row, line)

        if status != "completed":
            if not row["error_phase"] or not row["error_type"] or not row["error_message"]:
                raise ValueError(f"Row {line} terminal failure lacks error evidence")
            if any(row[field] != "" for field in METRIC_FIELDS):
                raise ValueError(f"Row {line} terminal failure contains fabricated metrics")
            if row["output_token_hash"] != "":
                raise ValueError(f"Row {line} terminal failure contains an output hash")
            continue

        if row["error_phase"] or row["error_type"] or row["error_message"]:
            raise ValueError(f"Row {line} completed case contains error evidence")
        non_interval_fields = METRIC_FIELDS.difference({"mean_tpot_ms", "p95_itl_ms"})
        values = {field: _number(row, field, line) for field in non_interval_fields}
        batch_size, output_tokens = key[1], key[3]
        if int(row["actual_output_tokens"]) != batch_size * output_tokens:
            raise ValueError(f"Row {line} has inconsistent actual_output_tokens")
        if values["generation_ms"] <= 0:
            raise ValueError(f"Row {line} generation_ms must be positive")
        if not _close(
            values["e2e_ttft_ms"],
            values["preprocessing_ms"] + values["h2d_ms"] + values["gpu_ttft_ms"],
        ):
            raise ValueError(f"Row {line} has inconsistent e2e_ttft_ms")
        if not _close(
            values["e2e_latency_ms"],
            values["preprocessing_ms"] + values["h2d_ms"] + values["generation_ms"],
        ):
            raise ValueError(f"Row {line} has inconsistent e2e_latency_ms")
        expected_token_rate = values["actual_output_tokens"] / (
            values["generation_ms"] / 1_000
        )
        expected_request_rate = batch_size / (values["generation_ms"] / 1_000)
        if not _close(values["output_tokens_per_second"], expected_token_rate):
            raise ValueError(f"Row {line} has inconsistent output token throughput")
        if not _close(values["requests_per_second"], expected_request_rate):
            raise ValueError(f"Row {line} has inconsistent request throughput")
        if output_tokens > 1:
            values["mean_tpot_ms"] = _number(row, "mean_tpot_ms", line)
            values["p95_itl_ms"] = _number(row, "p95_itl_ms", line)
            decode_ms = values["generation_ms"] - values["gpu_ttft_ms"]
            if decode_ms < 0 or not _close(
                values["mean_tpot_ms"], decode_ms / (output_tokens - 1)
            ):
                raise ValueError(f"Row {line} has inconsistent mean_tpot_ms")
        elif row["mean_tpot_ms"] != "" or row["p95_itl_ms"] != "":
            raise ValueError(f"Row {line} must leave TPOT/ITL blank for one output token")
        if not re.fullmatch(r"[0-9a-f]{64}", row["output_token_hash"]):
            raise ValueError(f"Row {line} has an invalid output_token_hash")
        workload = (batch_size, key[2], output_tokens)
        previous_hash = output_hashes.setdefault(workload, row["output_token_hash"])
        if previous_hash != row["output_token_hash"]:
            raise ValueError(
                f"Deterministic output mismatch for workload {workload}: "
                f"{previous_hash} != {row['output_token_hash']}"
            )

    missing = expected.difference(seen)
    if require_complete and missing:
        raise ValueError(
            f"Week 2 matrix is incomplete: {len(seen)}/{len(expected)} cases; "
            f"first missing cases: {sorted(missing)[:5]}"
        )
    return seen


def _ordered_workloads(
    config: Mapping[str, object],
) -> dict[str, list[tuple[str, int, int, int]]]:
    ordered: dict[str, list[tuple[str, int, int, int]]] = {}
    for case in iter_case_specs(config):
        workload = (case.sweep, case.batch_size, case.prompt_tokens, case.output_tokens)
        sweep_workloads = ordered.setdefault(case.sweep, [])
        if workload not in sweep_workloads:
            sweep_workloads.append(workload)
    return ordered


def validate_official_completion(
    rows: Iterable[Mapping[str, str]],
    config: Mapping[str, object],
) -> dict[tuple[str, int, int, int], str]:
    """Enforce the official success/capacity-boundary policy.

    Structural validation intentionally retains diagnostic ``error`` rows. Official
    completion is stricter: every point is uniformly successful or uniformly OOM,
    and OOM-only points form a monotonic suffix after at least one successful point.
    """

    row_list = list(rows)
    expected = expected_cases(config)
    actual = {case_key(row) for row in row_list}
    if actual != expected or len(row_list) != len(expected):
        raise ValueError(
            f"Official Week 2 completion requires all {len(expected)} unique attempts"
        )
    benchmark = config.get("benchmark")
    if not isinstance(benchmark, Mapping):
        raise ValueError("benchmark must be a mapping")
    repeats = int(benchmark["repeats"])
    grouped: dict[tuple[str, int, int, int], list[Mapping[str, str]]] = {}
    for row in row_list:
        key = (
            str(row["sweep"]),
            int(row["batch_size"]),
            int(row["prompt_tokens"]),
            int(row["output_tokens"]),
        )
        grouped.setdefault(key, []).append(row)

    point_states: dict[tuple[str, int, int, int], str] = {}
    for workload, attempts in grouped.items():
        statuses = [row["status"] for row in attempts]
        if len(attempts) != repeats:
            raise ValueError(
                f"Workload {workload} has {len(attempts)}/{repeats} terminal attempts"
            )
        if "error" in statuses:
            raise ValueError(f"Workload {workload} contains an unexpected error row")
        if all(status == "completed" for status in statuses):
            point_states[workload] = "completed"
        elif all(status == "oom" for status in statuses):
            if any(row.get("error_phase") != "measurement" for row in attempts):
                raise ValueError(
                    f"OOM boundary {workload} contains an unmeasured warmup failure"
                )
            point_states[workload] = "capacity_limited"
        else:
            raise ValueError(
                f"Workload {workload} mixes completed and OOM attempts; "
                "it is not a stable capacity boundary"
            )

    equivalent_states: dict[tuple[int, int, int], set[str]] = {}
    for workload, state in point_states.items():
        dimensions = workload[1:]
        equivalent_states.setdefault(dimensions, set()).add(state)
    inconsistent = {
        dimensions: states
        for dimensions, states in equivalent_states.items()
        if len(states) > 1
    }
    if inconsistent:
        raise ValueError(
            "Equivalent overlapping sweep points have inconsistent capacity states: "
            f"{inconsistent}"
        )

    for sweep, workloads in _ordered_workloads(config).items():
        saw_success = False
        saw_boundary = False
        for workload in workloads:
            state = point_states[workload]
            if state == "completed":
                if saw_boundary:
                    raise ValueError(
                        f"Sweep {sweep} succeeds after an OOM capacity boundary"
                    )
                saw_success = True
            else:
                if not saw_success:
                    raise ValueError(
                        f"Sweep {sweep} has no successful point before its OOM boundary"
                    )
                saw_boundary = True
    return point_states
