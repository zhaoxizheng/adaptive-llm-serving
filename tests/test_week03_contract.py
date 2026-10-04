from __future__ import annotations

import json
import threading
import time
from copy import deepcopy

import pytest

from src.common import load_yaml
from src.serve_week03 import BackendTiming, FakeBatchBackend, run_online_case, simulate_case
from src.week03_contract import (
    BATCH_FIELDS,
    EVENT_FIELDS,
    CaseKey,
    case_id,
    config_fingerprint,
    create_run_metadata,
    expand_matrix,
    validate_case_artifacts,
    validate_run_metadata,
)
from src.workload import TraceRequest


def config():
    return load_yaml("configs/week03.yaml")


def test_matrix_is_policy_specific_and_bounded() -> None:
    cases = expand_matrix(config(), "primary")

    assert len(cases) == 55
    assert all((case.max_batch_size, case.delay_ms) == (1, 0) for case in cases if case.policy == "no_batching")
    assert len(cases) <= 60


def test_config_fingerprint_excludes_output_paths() -> None:
    left = config()
    right = config()
    right["output"]["root"] = "/tmp/elsewhere"

    assert config_fingerprint(left) == config_fingerprint(right)


def test_fake_case_has_valid_cross_file_contract() -> None:
    cfg = config()
    case = CaseKey("smoke", "0.75", 0, "size_or_time", 8, 10)
    metadata = create_run_metadata(cfg, profile="smoke", backend="fake")
    trace = tuple(
        TraceRequest(index, f"r{index}", index * 1_000_000, 256, 64) for index in range(4)
    )
    backend = FakeBatchBackend({1: 10, 4: 20, 8: 30})

    events, batches = simulate_case(
        trace=trace, case=case, config=cfg, metadata=metadata, backend=backend, arrival_rate_rps=18.75
    )

    assert list(events[0]) == EVENT_FIELDS
    assert list(batches[0]) == BATCH_FIELDS
    validate_case_artifacts(events, batches, expected_run_id=str(metadata["run_id"]), expected_case_id=case_id(case))
    assert json.loads(str(batches[0]["request_ids_json"])) == ["r0", "r1", "r2", "r3"]


def test_validator_rejects_inconsistent_derived_latency() -> None:
    cfg = config()
    case = CaseKey("smoke", "0.75", 0, "no_batching", 1, 0)
    metadata = create_run_metadata(cfg, profile="smoke", backend="fake")
    events, batches = simulate_case(
        trace=(TraceRequest(0, "r0", 0, 256, 64),),
        case=case,
        config=cfg,
        metadata=metadata,
        backend=FakeBatchBackend({1: 10}),
        arrival_rate_rps=18.75,
    )
    events[0]["ttft_ns"] = int(str(events[0]["ttft_ns"])) + 1

    with pytest.raises(ValueError, match="inconsistent ttft_ns"):
        validate_case_artifacts(events, batches)


def test_case_contract_rejects_mixed_trace_and_case_dimensions() -> None:
    cfg = config()
    case = CaseKey("smoke", "0.75", 0, "no_batching", 1, 0)
    metadata = create_run_metadata(cfg, profile="smoke", backend="fake")
    events, batches = simulate_case(
        trace=(
            TraceRequest(0, "r0", 0, 256, 64),
            TraceRequest(1, "r1", 1_000_000, 256, 64),
        ),
        case=case,
        config=cfg,
        metadata=metadata,
        backend=FakeBatchBackend({1: 1}),
        arrival_rate_rps=18.75,
    )

    mixed_trace = deepcopy(events)
    mixed_trace[1]["trace_id"] = "tampered"
    with pytest.raises(ValueError, match="mixed case/config/trace identities"):
        validate_case_artifacts(mixed_trace, batches)

    mixed_case = deepcopy(events)
    mixed_case[1]["delay_ms"] = 1
    with pytest.raises(ValueError, match="case_id does not match"):
        validate_case_artifacts(mixed_case, batches)


def test_metadata_fingerprint_detects_provenance_tampering() -> None:
    cfg = config()
    metadata = create_run_metadata(cfg, profile="smoke", backend="fake")
    metadata["runtime"]["platform"] = "tampered"

    with pytest.raises(ValueError, match="runtime_fingerprint"):
        validate_run_metadata(metadata, cfg)


def test_resume_rejects_changed_runtime_and_calibration() -> None:
    cfg = config()
    metadata = _hf_metadata(cfg)
    original_runtime = dict(metadata["runtime"])
    original_calibration = dict(metadata["calibration"])

    with pytest.raises(ValueError, match="runtime identity changed"):
        validate_run_metadata(
            metadata, cfg, runtime={**original_runtime, "backend": "changed"}
        )
    with pytest.raises(ValueError, match="calibration identity changed"):
        validate_run_metadata(
            metadata,
            cfg,
            calibration={**original_calibration, "capacity_rps": 99.0},
        )


class _SleepingHFBackend:
    name = "hf"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def execute(self, batch) -> BackendTiming:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(0.002)
            return BackendTiming(service_ns=2_000_000, first_token_ns=500_000)
        finally:
            with self._lock:
                self._active -= 1


class _FirstSlowHFBackend:
    name = "hf"

    def __init__(self, first_service_seconds: float) -> None:
        self._first_service_seconds = first_service_seconds
        self._calls = 0

    def execute(self, batch) -> BackendTiming:
        del batch
        service_seconds = (
            self._first_service_seconds if self._calls == 0 else 0.001
        )
        self._calls += 1
        time.sleep(service_seconds)
        return BackendTiming(
            service_ns=round(service_seconds * 1_000_000_000),
            first_token_ns=0,
        )


def _hf_metadata(cfg, profile: str = "smoke"):
    source = {
        "git_commit": "a" * 40,
        "git_dirty": False,
        "critical_source_dirty": False,
        "dirty_state_fingerprint": "b" * 64,
        "source_tree_fingerprint": "c" * 64,
    }
    calibration = {
        "calibration_id": "d" * 64,
        "calibration_fingerprint": "d" * 64,
        "method": "batch1_median_completed_requests_per_second",
        "capacity_rps": 25.0,
    }
    return create_run_metadata(
        cfg,
        profile=profile,
        backend="hf",
        source=source,
        runtime={"backend": "hf-test"},
        calibration=calibration,
    )


def test_online_hf_path_uses_monotonic_lifecycle_and_one_worker() -> None:
    cfg = config()
    case = CaseKey("smoke", "0.75", 0, "no_batching", 1, 0)
    metadata = _hf_metadata(cfg)
    backend = _SleepingHFBackend()
    trace = tuple(
        TraceRequest(index, f"r{index}", index * 500_000, 256, 64)
        for index in range(3)
    )

    events, batches = run_online_case(
        trace=trace,
        case=case,
        config=cfg,
        metadata=metadata,
        backend=backend,
        arrival_rate_rps=18.75,
    )

    assert backend.max_active == 1
    assert len(events) == len(batches) == 3
    assert all(row["status"] == "completed" for row in events)
    assert all(int(row["observed_arrival_ns"]) >= int(row["scheduled_arrival_ns"]) for row in events)
    assert all(int(row["service_time_ns"]) > 0 for row in events)
    validate_case_artifacts(
        events,
        batches,
        expected_metadata=metadata,
        expected_case=case,
    )


def test_online_busy_worker_keeps_requests_unformed_until_worker_is_free() -> None:
    cfg = config()
    case = CaseKey("smoke", "0.75", 0, "no_batching", 1, 0)
    metadata = _hf_metadata(cfg)
    trace = tuple(
        TraceRequest(index, f"r{index}", index * 100_000, 256, 64)
        for index in range(3)
    )

    events, batches = run_online_case(
        trace=trace,
        case=case,
        config=cfg,
        metadata=metadata,
        backend=_SleepingHFBackend(),
        arrival_rate_rps=18.75,
    )

    worker_waits = [int(row["worker_wait_ns"]) for row in events]
    service_times = [int(row["service_time_ns"]) for row in events]
    # Thread handoff overhead is real worker wait, but a busy GPU service interval
    # must remain in queueing_delay instead of becoming an intermediate-batch wait.
    assert all(wait < service_times[0] for wait in worker_waits)
    assert int(events[1]["queueing_delay_ns"]) > 0
    assert int(events[2]["queueing_delay_ns"]) > 0
    assert all(
        int(row["dispatch_ns"]) <= int(row["gpu_start_ns"])
        for row in batches
    )
    assert all(
        int(right["gpu_start_ns"]) >= int(left["finished_ns"])
        for left, right in zip(batches, batches[1:])
    )


def test_online_fixed_window_skips_boundaries_while_worker_is_busy() -> None:
    cfg = config()
    case = CaseKey("smoke", "0.75", 0, "fixed_window", 8, 1)
    metadata = _hf_metadata(cfg)
    trace = (
        TraceRequest(0, "r0", 100_000, 256, 64),
        TraceRequest(1, "r1", 2_100_000, 256, 64),
        TraceRequest(2, "r2", 2_200_000, 256, 64),
    )

    events, batches = run_online_case(
        trace=trace,
        case=case,
        config=cfg,
        metadata=metadata,
        backend=_SleepingHFBackend(),
        arrival_rate_rps=18.75,
    )

    assert len(batches) == 2
    assert json.loads(str(batches[0]["request_ids_json"])) == ["r0"]
    assert json.loads(str(batches[1]["request_ids_json"])) == ["r1", "r2"]
    assert int(batches[1]["dispatch_ns"]) >= int(batches[0]["finished_ns"])
    assert all(
        int(row["worker_wait_ns"]) < int(row["service_time_ns"])
        for row in events
    )


def test_online_size_or_time_dispatches_at_worker_availability_not_old_deadline() -> None:
    cfg = config()
    case = CaseKey("smoke", "0.75", 0, "size_or_time", 1, 1)
    metadata = _hf_metadata(cfg)
    trace = (
        TraceRequest(0, "r0", 0, 256, 64),
        TraceRequest(1, "r1", 100_000, 256, 64),
    )

    events, batches = run_online_case(
        trace=trace,
        case=case,
        config=cfg,
        metadata=metadata,
        backend=_SleepingHFBackend(),
        arrival_rate_rps=18.75,
    )

    assert len(batches) == 2
    assert int(batches[1]["dispatch_ns"]) >= int(batches[0]["finished_ns"])
    assert int(batches[1]["dispatch_ns"]) <= int(batches[1]["gpu_start_ns"])
    assert batches[1]["trigger"] in {
        "worker_became_available_size",
        "size",
        "shutdown",
    }
    assert int(events[1]["queueing_delay_ns"]) > 0


def test_online_equal_timestamp_arrivals_join_before_flush() -> None:
    cfg = config()
    case = CaseKey("smoke", "0.75", 0, "size_or_time", 3, 10)
    metadata = _hf_metadata(cfg)
    trace = tuple(
        TraceRequest(index, f"r{index}", 500_000, 256, 64)
        for index in range(3)
    )

    _, batches = run_online_case(
        trace=trace,
        case=case,
        config=cfg,
        metadata=metadata,
        backend=_SleepingHFBackend(),
        arrival_rate_rps=18.75,
    )

    assert len(batches) == 1
    assert batches[0]["trigger"] == "size"
    assert json.loads(str(batches[0]["request_ids_json"])) == ["r0", "r1", "r2"]


def test_online_full_queue_observes_all_arrivals_before_independent_timeouts() -> None:
    cfg = config()
    cfg["scheduler"]["queue_capacity"] = 1
    cfg["scheduler"]["admission_timeout_ms"] = 40
    case = CaseKey("smoke", "0.75", 0, "no_batching", 1, 0)
    metadata = _hf_metadata(cfg)
    trace = (
        TraceRequest(0, "r0", 0, 256, 64),
        TraceRequest(1, "r1", 1_000_000, 256, 64),
        TraceRequest(2, "r2", 2_000_000, 256, 64),
        TraceRequest(3, "r3", 3_000_000, 256, 64),
    )

    events, _ = run_online_case(
        trace=trace,
        case=case,
        config=cfg,
        metadata=metadata,
        backend=_FirstSlowHFBackend(0.120),
        arrival_rate_rps=18.75,
    )

    by_id = {str(row["request_id"]): row for row in events}
    rejected = [by_id["r2"], by_id["r3"]]
    assert [row["status"] for row in rejected] == ["rejected", "rejected"]
    assert max(int(row["observed_arrival_ns"]) for row in rejected) < min(
        int(row["terminal_ns"]) for row in rejected
    )
    for row in rejected:
        assert row["failure_reason"] == "queue_capacity_timeout"
        assert row["admitted_ns"] == ""
        assert int(row["arrival_lag_ns"]) == (
            int(row["observed_arrival_ns"])
            - int(row["scheduled_arrival_ns"])
        )
        assert (
            int(row["terminal_ns"]) - int(row["observed_arrival_ns"])
            >= 40_000_000
        )


def test_online_later_waiter_can_admit_after_earlier_independent_timeout() -> None:
    cfg = config()
    cfg["scheduler"]["queue_capacity"] = 1
    cfg["scheduler"]["admission_timeout_ms"] = 120
    case = CaseKey("smoke", "0.75", 0, "no_batching", 1, 0)
    metadata = _hf_metadata(cfg)
    trace = (
        TraceRequest(0, "r0", 0, 256, 64),
        TraceRequest(1, "r1", 1_000_000, 256, 64),
        TraceRequest(2, "r2", 2_000_000, 256, 64),
        TraceRequest(3, "r3", 60_000_000, 256, 64),
    )

    events, _ = run_online_case(
        trace=trace,
        case=case,
        config=cfg,
        metadata=metadata,
        backend=_FirstSlowHFBackend(0.150),
        arrival_rate_rps=18.75,
    )

    by_id = {str(row["request_id"]): row for row in events}
    earlier = by_id["r2"]
    later = by_id["r3"]
    assert earlier["status"] == "rejected"
    assert later["status"] == "completed"
    assert int(later["observed_arrival_ns"]) < int(earlier["terminal_ns"])
    assert int(earlier["terminal_ns"]) < int(later["admitted_ns"])
    assert 0 < int(later["admission_wait_ns"]) < 120_000_000


def test_simulator_refuses_to_label_hf_measurements_as_online_evidence() -> None:
    cfg = config()
    case = CaseKey("smoke", "0.75", 0, "no_batching", 1, 0)

    with pytest.raises(ValueError, match="simulation-only"):
        simulate_case(
            trace=(TraceRequest(0, "r0", 0, 256, 64),),
            case=case,
            config=cfg,
            metadata=_hf_metadata(cfg),
            backend=_SleepingHFBackend(),
            arrival_rate_rps=18.75,
        )
