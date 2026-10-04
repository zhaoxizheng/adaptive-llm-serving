from __future__ import annotations

import json
import math
import platform
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Mapping, Sequence

from src.common import stable_fingerprint, utc_now

SCHEMA_VERSION = 2
PINNED_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
POLICIES = ("no_batching", "fixed_window", "size_or_time")
STATUSES = {"completed", "rejected", "failed"}
CALIBRATION_METHOD = "batch1_median_completed_requests_per_second"

EVENT_FIELDS = [
    "schema_version",
    "run_id",
    "metadata_fingerprint",
    "config_fingerprint",
    "calibration_id",
    "case_id",
    "trace_id",
    "profile",
    "policy",
    "offered_load_ratio",
    "arrival_rate_rps",
    "repeat",
    "max_batch_size",
    "delay_ms",
    "request_id",
    "ordinal",
    "measurement",
    "scheduled_arrival_ns",
    "observed_arrival_ns",
    "admitted_ns",
    "dispatch_ns",
    "gpu_start_ns",
    "first_token_ns",
    "finished_ns",
    "terminal_ns",
    "status",
    "batch_id",
    "prompt_tokens",
    "output_tokens",
    "queue_depth_at_admission",
    "failure_reason",
    "arrival_lag_ns",
    "admission_wait_ns",
    "queueing_delay_ns",
    "worker_wait_ns",
    "service_time_ns",
    "ttft_ns",
    "e2e_latency_ns",
]

BATCH_FIELDS = [
    "schema_version",
    "run_id",
    "metadata_fingerprint",
    "config_fingerprint",
    "calibration_id",
    "case_id",
    "trace_id",
    "profile",
    "policy",
    "offered_load_ratio",
    "arrival_rate_rps",
    "repeat",
    "max_batch_size",
    "delay_ms",
    "batch_id",
    "trigger",
    "request_ids_json",
    "request_count",
    "dispatch_ns",
    "gpu_start_ns",
    "first_token_ns",
    "finished_ns",
    "service_time_ns",
    "fill_ratio",
    "queue_depth_at_dispatch",
    "status",
    "failure_reason",
]


@dataclass(frozen=True, order=True, slots=True)
class CaseKey:
    profile: str
    offered_load_ratio: str
    repeat: int
    policy: str
    max_batch_size: int
    delay_ms: int

    @property
    def ratio(self) -> float:
        return float(self.offered_load_ratio)


def canonical_ratio(value: object) -> str:
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"invalid offered load ratio: {value!r}") from error
    if not number.is_finite() or number <= 0:
        raise ValueError("offered load ratio must be finite and positive")
    return format(number.normalize(), "f")


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def scientific_config(config: Mapping[str, object]) -> dict[str, object]:
    required = ("model", "workload", "calibration", "scheduler", "matrix", "fake_backend")
    missing = [name for name in required if name not in config]
    if missing:
        raise ValueError(f"Week 3 config is missing sections: {missing}")
    snapshot = {name: deepcopy(config[name]) for name in required}
    model = _mapping(snapshot["model"], "model")
    revision = str(model.get("revision", ""))
    if not PINNED_REVISION_PATTERN.fullmatch(revision):
        raise ValueError(
            "model.revision must be an immutable 40-character Hugging Face commit SHA"
        )
    workload = _mapping(snapshot["workload"], "workload")
    duration = float(workload.get("duration_seconds", 0))
    warmup = float(workload.get("warmup_seconds", -1))
    if not math.isfinite(duration) or duration <= 0 or warmup < 0 or warmup >= duration:
        raise ValueError("workload duration must be positive and include a shorter warmup")
    scheduler = _mapping(snapshot["scheduler"], "scheduler")
    if int(scheduler.get("queue_capacity", 0)) <= 0:
        raise ValueError("scheduler.queue_capacity must be positive")
    calibration = _mapping(snapshot["calibration"], "calibration")
    if "capacity_rps" in calibration:
        raise ValueError(
            "calibration.capacity_rps is a manual placeholder; use a measured artifact"
        )
    if calibration.get("method") != CALIBRATION_METHOD:
        raise ValueError(f"calibration.method must be {CALIBRATION_METHOD!r}")
    sizes = calibration.get("static_batch_sizes")
    if (
        not isinstance(sizes, list)
        or not sizes
        or any(isinstance(value, bool) or int(value) <= 0 for value in sizes)
        or len({int(value) for value in sizes}) != len(sizes)
        or 1 not in {int(value) for value in sizes}
    ):
        raise ValueError("calibration.static_batch_sizes needs unique positive sizes including 1")
    if not str(calibration.get("artifact", "")).strip():
        raise ValueError("calibration.artifact must identify measured calibration evidence")
    # Artifact locations are operational, while the method and required batch sizes
    # are scientific inputs. This keeps relocated evidence resumable.
    snapshot["calibration"] = {
        "method": calibration["method"],
        "static_batch_sizes": [int(value) for value in sizes],
    }
    fake = _mapping(snapshot["fake_backend"], "fake_backend")
    fake_capacity = float(fake.get("capacity_rps", 0))
    if not math.isfinite(fake_capacity) or fake_capacity <= 0:
        raise ValueError("fake_backend.capacity_rps must be finite and positive")
    # JSON is the persisted metadata representation. Canonicalize YAML integer
    # mapping keys (notably fake batch-size service curves) now so an identical
    # config compares equal after a write/read resume cycle.
    return json.loads(json.dumps(snapshot, sort_keys=True))


def config_fingerprint(config: Mapping[str, object]) -> str:
    return stable_fingerprint(scientific_config(config))


def _policy_defaults(matrix: Mapping[str, object], policy: str) -> tuple[int, int]:
    defaults = _mapping(matrix.get("policy_defaults"), "matrix.policy_defaults")
    value = _mapping(defaults.get(policy), f"matrix.policy_defaults.{policy}")
    max_batch_size = int(value.get("max_batch_size", 0))
    delay_ms = int(value.get("delay_ms", -1))
    if max_batch_size <= 0 or delay_ms < 0:
        raise ValueError(f"invalid defaults for policy {policy}")
    if policy == "no_batching" and (max_batch_size, delay_ms) != (1, 0):
        raise ValueError("no_batching defaults must be max_batch_size=1 and delay_ms=0")
    return max_batch_size, delay_ms


def _add_case(cases: set[CaseKey], case: CaseKey) -> None:
    if case in cases:
        return
    if case.policy not in POLICIES:
        raise ValueError(f"unsupported Week 3 policy: {case.policy}")
    if case.repeat < 0 or case.max_batch_size <= 0 or case.delay_ms < 0:
        raise ValueError(f"invalid Week 3 case: {case}")
    cases.add(case)


def expand_matrix(
    config: Mapping[str, object], profile: str = "primary"
) -> tuple[CaseKey, ...]:
    scientific_config(config)
    matrix = _mapping(config["matrix"], "matrix")
    section = _mapping(matrix.get(profile), f"matrix.{profile}")
    loads = [canonical_ratio(value) for value in section.get("offered_load_ratio", [])]
    repeats = int(section.get("repeats", 0))
    policies = [str(value) for value in section.get("policies", POLICIES)]
    if not loads or repeats <= 0:
        raise ValueError(f"matrix.{profile} needs offered loads and positive repeats")
    cases: set[CaseKey] = set()
    for load in loads:
        for repeat in range(repeats):
            for policy in policies:
                size, delay = _policy_defaults(matrix, policy)
                _add_case(cases, CaseKey(profile, load, repeat, policy, size, delay))

    sweep = section.get("parameter_sweep")
    if sweep:
        sweep_map = _mapping(sweep, f"matrix.{profile}.parameter_sweep")
        sweep_load = canonical_ratio(sweep_map["offered_load_ratio"])
        sweep_repeats = int(sweep_map.get("repeats", 1))
        for repeat in range(sweep_repeats):
            for policy in sweep_map.get("policies", ["fixed_window", "size_or_time"]):
                for size in sweep_map.get("max_batch_size", []):
                    for delay in sweep_map.get("delay_ms", []):
                        _add_case(
                            cases,
                            CaseKey(
                                profile,
                                sweep_load,
                                repeat,
                                str(policy),
                                int(size),
                                int(delay),
                            ),
                        )

    maximum = int(section.get("max_cases", matrix.get("max_cases", 0)))
    if maximum <= 0 or len(cases) > maximum:
        raise ValueError(
            f"Week 3 {profile} matrix has {len(cases)} cases, exceeding max_cases={maximum}"
        )
    return tuple(sorted(cases))


def case_id(case: CaseKey) -> str:
    ratio = case.offered_load_ratio.replace(".", "p")
    return (
        f"{case.profile}-load-{ratio}-repeat-{case.repeat}-{case.policy}"
        f"-b{case.max_batch_size}-d{case.delay_ms}"
    )


def trace_key(case: CaseKey) -> tuple[str, str, int]:
    return case.profile, case.offered_load_ratio, case.repeat


def create_run_metadata(
    config: Mapping[str, object],
    *,
    profile: str,
    backend: str,
    source: Mapping[str, object] | None = None,
    runtime: Mapping[str, object] | None = None,
    calibration: Mapping[str, object] | None = None,
) -> dict[str, object]:
    cases = expand_matrix(config, profile)
    source_contract = None
    if source is not None:
        source_contract = {
            key: source.get(key)
            for key in (
                "git_commit",
                "git_dirty",
                "critical_source_dirty",
                "dirty_state_fingerprint",
                "source_tree_fingerprint",
            )
        }
    runtime_contract = dict(
        runtime
        or {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "backend": backend,
        }
    )
    if backend == "hf" and (source_contract is None or calibration is None):
        raise ValueError("HF runs require source and measured calibration provenance")
    if calibration is None:
        fake = _mapping(config["fake_backend"], "fake_backend")
        simulation = {
            "calibration_id": "simulation-config",
            "calibration_fingerprint": stable_fingerprint(fake, length=64),
            "capacity_rps": float(fake["capacity_rps"]),
            "method": "configured_fake_service_curve",
        }
        calibration_contract = simulation
    else:
        calibration_contract = dict(calibration)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(uuid.uuid4()),
        "started_at": utc_now(),
        "profile": profile,
        "backend": backend,
        "source": source_contract,
        "runtime": runtime_contract,
        "runtime_fingerprint": stable_fingerprint(runtime_contract),
        "calibration": calibration_contract,
        "calibration_id": calibration_contract.get("calibration_id"),
        "config_fingerprint": config_fingerprint(config),
        "scientific_config": scientific_config(config),
        "expected_case_count": len(cases),
    }
    metadata["metadata_fingerprint"] = metadata_fingerprint(metadata)
    return metadata


def metadata_fingerprint(metadata: Mapping[str, object]) -> str:
    payload = {
        key: deepcopy(value)
        for key, value in metadata.items()
        if key != "metadata_fingerprint"
    }
    return stable_fingerprint(payload, length=64)


def validate_run_metadata(
    metadata: Mapping[str, object],
    config: Mapping[str, object],
    *,
    profile: str | None = None,
    backend: str | None = None,
    source: Mapping[str, object] | None = None,
    runtime: Mapping[str, object] | None = None,
    calibration: Mapping[str, object] | None = None,
) -> None:
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported or missing Week 3 metadata schema_version")
    try:
        uuid.UUID(str(metadata["run_id"]))
    except (KeyError, ValueError) as error:
        raise ValueError("Week 3 metadata contains an invalid run_id") from error
    selected_profile = profile or str(metadata.get("profile", ""))
    if profile is not None and metadata.get("profile") != profile:
        raise ValueError("cannot resume with a different Week 3 matrix profile")
    if backend is not None and metadata.get("backend") != backend:
        raise ValueError("cannot resume with a different Week 3 backend")
    if metadata.get("config_fingerprint") != config_fingerprint(config):
        raise ValueError("Week 3 metadata does not match the scientific config")
    if metadata.get("scientific_config") != scientific_config(config):
        raise ValueError("Week 3 metadata scientific_config is inconsistent")
    if metadata.get("expected_case_count") != len(expand_matrix(config, selected_profile)):
        raise ValueError("Week 3 metadata expected_case_count is inconsistent")
    stored_runtime = _mapping(metadata.get("runtime"), "metadata.runtime")
    if metadata.get("runtime_fingerprint") != stable_fingerprint(stored_runtime):
        raise ValueError("Week 3 metadata runtime_fingerprint is inconsistent")
    stored_calibration = _mapping(
        metadata.get("calibration"), "metadata.calibration"
    )
    if (
        not metadata.get("calibration_id")
        or metadata.get("calibration_id")
        != stored_calibration.get("calibration_id")
    ):
        raise ValueError("Week 3 metadata calibration identity is inconsistent")
    if metadata.get("metadata_fingerprint") != metadata_fingerprint(metadata):
        raise ValueError("Week 3 metadata fingerprint is inconsistent")
    if source is not None:
        current_source = {
            key: source.get(key)
            for key in (
                "git_commit",
                "git_dirty",
                "critical_source_dirty",
                "dirty_state_fingerprint",
                "source_tree_fingerprint",
            )
        }
        if metadata.get("source") != current_source:
            raise ValueError("cannot resume: Week 3 source identity changed")
    if runtime is not None and (
        metadata.get("runtime") != dict(runtime)
        or metadata.get("runtime_fingerprint") != stable_fingerprint(runtime)
    ):
        raise ValueError("cannot resume: Week 3 runtime identity changed")
    if calibration is not None and metadata.get("calibration") != dict(calibration):
        raise ValueError("cannot resume: Week 3 calibration identity changed")


def validate_official_metadata(metadata: Mapping[str, object]) -> None:
    """Apply the non-negotiable provenance gates for formal Week 3 evidence."""

    if metadata.get("backend") != "hf" or metadata.get("profile") != "primary":
        raise ValueError("formal Week 3 evidence requires backend=hf and profile=primary")
    source = _mapping(metadata.get("source"), "metadata.source")
    if not PINNED_REVISION_PATTERN.fullmatch(str(source.get("git_commit", ""))):
        raise ValueError("formal Week 3 evidence has an invalid source Git commit")
    if source.get("critical_source_dirty", source.get("git_dirty")) is not False:
        raise ValueError("formal Week 3 evidence requires clean committed experiment source")
    if not SHA256_PATTERN.fullmatch(str(source.get("source_tree_fingerprint", ""))):
        raise ValueError("formal Week 3 evidence lacks a source-tree fingerprint")
    runtime = _mapping(metadata.get("runtime"), "metadata.runtime")
    required_runtime = (
        "dependency_freeze_sha256",
        "model_snapshot_fingerprint",
        "cuda_runtime",
        "driver",
        "gpu_names",
        "gpu_count",
        "model",
        "model_revision",
        "dtype",
    )
    missing = [name for name in required_runtime if runtime.get(name) in (None, "", [])]
    if missing or int(runtime.get("gpu_count", 0)) < 1:
        raise ValueError(f"formal Week 3 evidence lacks CUDA/runtime fields: {missing}")
    calibration = _mapping(metadata.get("calibration"), "metadata.calibration")
    if calibration.get("method") != CALIBRATION_METHOD:
        raise ValueError("formal Week 3 evidence lacks measured calibration provenance")
    if calibration.get("calibration_id") != metadata.get("calibration_id"):
        raise ValueError("formal Week 3 calibration identity is inconsistent")
    if not SHA256_PATTERN.fullmatch(
        str(calibration.get("calibration_fingerprint", ""))
    ):
        raise ValueError("formal Week 3 calibration fingerprint is invalid")


def _require_exact_fields(
    row: Mapping[str, object], fields: Sequence[str], kind: str, line: int
) -> None:
    if list(row.keys()) != list(fields) and set(row) != set(fields):
        missing = sorted(set(fields).difference(row))
        extra = sorted(set(row).difference(fields))
        raise ValueError(f"{kind} row {line} schema mismatch; missing={missing}, extra={extra}")


def _integer(row: Mapping[str, object], field: str, line: int, *, blank: bool = False) -> int | None:
    raw = row.get(field, "")
    if raw in ("", None):
        if blank:
            return None
        raise ValueError(f"row {line} has blank required integer {field}")
    try:
        value = int(str(raw))
    except ValueError as error:
        raise ValueError(f"row {line} has invalid integer {field}: {raw!r}") from error
    if value < 0:
        raise ValueError(f"row {line} has negative {field}")
    return value


def _float(row: Mapping[str, object], field: str, line: int) -> float:
    try:
        value = float(str(row[field]))
    except (KeyError, ValueError) as error:
        raise ValueError(f"row {line} has invalid float {field}") from error
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"row {line} has non-finite or negative {field}")
    return value


def _parse_bool(value: object) -> bool:
    normalized = str(value).lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def _row_case(row: Mapping[str, object], line: int) -> CaseKey:
    try:
        case = CaseKey(
            profile=str(row["profile"]),
            offered_load_ratio=canonical_ratio(row["offered_load_ratio"]),
            repeat=int(str(row["repeat"])),
            policy=str(row["policy"]),
            max_batch_size=int(str(row["max_batch_size"])),
            delay_ms=int(str(row["delay_ms"])),
        )
        _add_case(set(), case)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"row {line} has an invalid case identity") from error
    if str(row.get("case_id", "")) != case_id(case):
        raise ValueError(f"row {line} case_id does not match its case dimensions")
    return case


def _identity_tuple(row: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(
        str(row[field])
        for field in (
            "run_id",
            "metadata_fingerprint",
            "config_fingerprint",
            "calibration_id",
            "case_id",
            "trace_id",
            "profile",
            "policy",
            "offered_load_ratio",
            "arrival_rate_rps",
            "repeat",
            "max_batch_size",
            "delay_ms",
        )
    )


def _validate_row_identity(
    row: Mapping[str, object],
    line: int,
    *,
    expected_run_id: str | None,
    expected_case_id: str | None,
    expected_metadata: Mapping[str, object] | None,
    expected_case: CaseKey | None,
    expected_trace_id: str | None,
) -> tuple[str, ...]:
    case = _row_case(row, line)
    if expected_run_id is not None and str(row["run_id"]) != expected_run_id:
        raise ValueError(f"row {line} has a mixed run_id")
    if expected_case_id is not None and str(row["case_id"]) != expected_case_id:
        raise ValueError(f"row {line} has a mixed case_id")
    if expected_case is not None and case != expected_case:
        raise ValueError(f"row {line} does not match the expected case dimensions")
    if expected_trace_id is not None and str(row["trace_id"]) != expected_trace_id:
        raise ValueError(f"row {line} has a mixed trace_id")
    if not str(row.get("trace_id", "")):
        raise ValueError(f"row {line} has a blank trace_id")
    if expected_metadata is not None:
        expected = {
            "run_id": expected_metadata.get("run_id"),
            "metadata_fingerprint": expected_metadata.get("metadata_fingerprint"),
            "config_fingerprint": expected_metadata.get("config_fingerprint"),
            "calibration_id": expected_metadata.get("calibration_id"),
        }
        for field, value in expected.items():
            if str(row.get(field, "")) != str(value):
                raise ValueError(f"row {line} identity mismatch for {field}")
    for field in ("metadata_fingerprint", "config_fingerprint", "calibration_id"):
        if not str(row.get(field, "")):
            raise ValueError(f"row {line} has a blank {field}")
    arrival_rate = _float(row, "arrival_rate_rps", line)
    if arrival_rate <= 0:
        raise ValueError(f"row {line} arrival_rate_rps must be positive")
    return _identity_tuple(row)


def validate_event_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    expected_run_id: str | None = None,
    expected_case_id: str | None = None,
    expected_metadata: Mapping[str, object] | None = None,
    expected_case: CaseKey | None = None,
    expected_trace_id: str | None = None,
) -> set[str]:
    seen: set[str] = set()
    identities: set[tuple[str, ...]] = set()
    for line, row in enumerate(rows, start=2):
        _require_exact_fields(row, EVENT_FIELDS, "event", line)
        if int(str(row["schema_version"])) != SCHEMA_VERSION:
            raise ValueError(f"event row {line} has unsupported schema_version")
        identities.add(
            _validate_row_identity(
                row,
                line,
                expected_run_id=expected_run_id,
                expected_case_id=expected_case_id,
                expected_metadata=expected_metadata,
                expected_case=expected_case,
                expected_trace_id=expected_trace_id,
            )
        )
        request_id = str(row["request_id"])
        if not request_id or request_id in seen:
            raise ValueError(f"duplicate or blank request_id in event row {line}")
        seen.add(request_id)
        status = str(row["status"])
        if status not in STATUSES:
            raise ValueError(f"event row {line} has invalid status {status!r}")
        _parse_bool(row["measurement"])
        scheduled = _integer(row, "scheduled_arrival_ns", line)
        observed = _integer(row, "observed_arrival_ns", line)
        terminal = _integer(row, "terminal_ns", line)
        assert scheduled is not None and observed is not None and terminal is not None
        if observed < scheduled or terminal < observed:
            raise ValueError(f"event row {line} violates arrival/terminal ordering")
        admitted = _integer(row, "admitted_ns", line, blank=True)
        dispatch = _integer(row, "dispatch_ns", line, blank=True)
        gpu_start = _integer(row, "gpu_start_ns", line, blank=True)
        first_token = _integer(row, "first_token_ns", line, blank=True)
        finished = _integer(row, "finished_ns", line, blank=True)
        optional_chain = [admitted, dispatch, gpu_start, first_token, finished]
        present = [value for value in optional_chain if value is not None]
        if any(value is None for value in optional_chain[: len(present)]):
            raise ValueError(f"event row {line} has a non-prefix lifecycle")
        if present != sorted(present) or (present and present[0] < observed):
            raise ValueError(f"event row {line} has out-of-order lifecycle timestamps")
        if status == "completed":
            if any(value is None for value in optional_chain):
                raise ValueError(f"completed event row {line} has blank lifecycle timestamps")
            if finished != terminal or not row["batch_id"] or row["failure_reason"]:
                raise ValueError(f"completed event row {line} has inconsistent terminal fields")
        elif status == "rejected":
            if any(value is not None for value in optional_chain) or row["batch_id"]:
                raise ValueError(f"rejected event row {line} must not have batch lifecycle fields")
            if not row["failure_reason"]:
                raise ValueError(f"rejected event row {line} needs a reason")
        else:
            if not row["failure_reason"]:
                raise ValueError(f"failed event row {line} needs a reason")
        expected_derived: dict[str, int | None] = {
            "arrival_lag_ns": observed - scheduled,
            "admission_wait_ns": None if admitted is None else admitted - observed,
            "queueing_delay_ns": None if dispatch is None or admitted is None else dispatch - admitted,
            "worker_wait_ns": None if gpu_start is None or dispatch is None else gpu_start - dispatch,
            "service_time_ns": None if finished is None or gpu_start is None else finished - gpu_start,
            "ttft_ns": None if first_token is None or admitted is None else first_token - admitted,
            "e2e_latency_ns": None if finished is None or admitted is None else finished - admitted,
        }
        for field, expected in expected_derived.items():
            actual = _integer(row, field, line, blank=True)
            if actual != expected:
                raise ValueError(
                    f"event row {line} has inconsistent {field}: expected {expected}, got {actual}"
                )
        if _integer(row, "prompt_tokens", line) in (None, 0) or _integer(
            row, "output_tokens", line
        ) in (None, 0):
            raise ValueError(f"event row {line} needs positive token counts")
    if len(identities) > 1:
        raise ValueError("event rows contain mixed case/config/trace identities")
    return seen


def validate_batch_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    expected_run_id: str | None = None,
    expected_case_id: str | None = None,
    expected_metadata: Mapping[str, object] | None = None,
    expected_case: CaseKey | None = None,
    expected_trace_id: str | None = None,
) -> dict[str, tuple[str, ...]]:
    batches: dict[str, tuple[str, ...]] = {}
    member_ids: set[str] = set()
    identities: set[tuple[str, ...]] = set()
    for line, row in enumerate(rows, start=2):
        _require_exact_fields(row, BATCH_FIELDS, "batch", line)
        if int(str(row["schema_version"])) != SCHEMA_VERSION:
            raise ValueError(f"batch row {line} has unsupported schema_version")
        identities.add(
            _validate_row_identity(
                row,
                line,
                expected_run_id=expected_run_id,
                expected_case_id=expected_case_id,
                expected_metadata=expected_metadata,
                expected_case=expected_case,
                expected_trace_id=expected_trace_id,
            )
        )
        batch_id = str(row["batch_id"])
        if not batch_id or batch_id in batches:
            raise ValueError(f"duplicate or blank batch_id in row {line}")
        try:
            decoded = json.loads(str(row["request_ids_json"]))
        except json.JSONDecodeError as error:
            raise ValueError(f"batch row {line} has invalid request_ids_json") from error
        if not isinstance(decoded, list) or not decoded or any(
            not isinstance(value, str) or not value for value in decoded
        ):
            raise ValueError(f"batch row {line} has invalid request membership")
        members = tuple(decoded)
        if str(row["request_ids_json"]) != json.dumps(decoded, separators=(",", ":")):
            raise ValueError(f"batch row {line} request_ids_json is not canonical")
        if member_ids.intersection(members):
            raise ValueError(f"request belongs to multiple batches at row {line}")
        member_ids.update(members)
        request_count = _integer(row, "request_count", line)
        max_batch_size = _integer(row, "max_batch_size", line)
        assert request_count is not None and max_batch_size is not None
        if request_count != len(members) or not 0 < request_count <= max_batch_size:
            raise ValueError(f"batch row {line} has inconsistent size")
        dispatch = _integer(row, "dispatch_ns", line)
        gpu_start = _integer(row, "gpu_start_ns", line)
        first_token = _integer(row, "first_token_ns", line, blank=True)
        finished = _integer(row, "finished_ns", line)
        assert dispatch is not None and gpu_start is not None and finished is not None
        if not dispatch <= gpu_start <= finished:
            raise ValueError(f"batch row {line} has out-of-order timestamps")
        if first_token is not None and not gpu_start <= first_token <= finished:
            raise ValueError(f"batch row {line} has an invalid first_token_ns")
        service = _integer(row, "service_time_ns", line)
        if service != finished - gpu_start:
            raise ValueError(f"batch row {line} has inconsistent service_time_ns")
        fill = _float(row, "fill_ratio", line)
        if not math.isclose(fill, request_count / max_batch_size, abs_tol=1e-12):
            raise ValueError(f"batch row {line} has inconsistent fill_ratio")
        status = str(row["status"])
        if status not in {"completed", "failed"}:
            raise ValueError(f"batch row {line} has invalid status")
        if (status == "failed") != bool(row["failure_reason"]):
            raise ValueError(f"batch row {line} has inconsistent failure_reason")
        if status == "completed" and first_token is None:
            raise ValueError(f"completed batch row {line} has no first_token_ns")
        batches[batch_id] = members
    if len(identities) > 1:
        raise ValueError("batch rows contain mixed case/config/trace identities")
    return batches


def validate_case_artifacts(
    event_rows: Sequence[Mapping[str, object]],
    batch_rows: Sequence[Mapping[str, object]],
    *,
    expected_run_id: str | None = None,
    expected_case_id: str | None = None,
    expected_metadata: Mapping[str, object] | None = None,
    expected_case: CaseKey | None = None,
    expected_trace_id: str | None = None,
) -> None:
    event_ids = validate_event_rows(
        event_rows,
        expected_run_id=expected_run_id,
        expected_case_id=expected_case_id,
        expected_metadata=expected_metadata,
        expected_case=expected_case,
        expected_trace_id=expected_trace_id,
    )
    batches = validate_batch_rows(
        batch_rows,
        expected_run_id=expected_run_id,
        expected_case_id=expected_case_id,
        expected_metadata=expected_metadata,
        expected_case=expected_case,
        expected_trace_id=expected_trace_id,
    )
    if event_rows and batch_rows and _identity_tuple(event_rows[0]) != _identity_tuple(batch_rows[0]):
        raise ValueError("event and batch files have different case/config/trace identities")
    events_by_id = {str(row["request_id"]): row for row in event_rows}
    batched_ids = {member for members in batches.values() for member in members}
    expected_batched = {
        request_id
        for request_id, row in events_by_id.items()
        if str(row["status"]) != "rejected"
    }
    if batched_ids != expected_batched:
        raise ValueError(
            "event/batch membership differs: "
            f"missing={sorted(expected_batched - batched_ids)}, "
            f"extra={sorted(batched_ids - expected_batched)}"
        )
    if event_ids != set(events_by_id):
        raise ValueError("event request identity is inconsistent")
    for batch_id, members in batches.items():
        batch = next(row for row in batch_rows if row["batch_id"] == batch_id)
        member_rows = [events_by_id[member] for member in members]
        for event in member_rows:
            if event["batch_id"] != batch_id:
                raise ValueError(f"event {event['request_id']} references the wrong batch")
            for field in ("dispatch_ns", "gpu_start_ns"):
                if event[field] != batch[field]:
                    raise ValueError(f"event/batch {field} mismatch for {batch_id}")
            if event["status"] != batch["status"]:
                raise ValueError(f"event/batch status mismatch for {batch_id}")
            if event["failure_reason"] != batch["failure_reason"]:
                raise ValueError(f"event/batch failure_reason mismatch for {batch_id}")
            if batch["status"] == "completed":
                for field in ("first_token_ns", "finished_ns"):
                    if event[field] != batch[field]:
                        raise ValueError(f"event/batch {field} mismatch for {batch_id}")
        terminal_values = [int(str(event["terminal_ns"])) for event in member_rows]
        if int(str(batch["finished_ns"])) != max(terminal_values):
            raise ValueError(f"batch {batch_id} finish is not its last member terminal")


def artifact_hash(payload: bytes) -> str:
    return sha256(payload).hexdigest()
