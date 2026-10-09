from __future__ import annotations

import argparse
import csv
import json
import math
import os
import queue
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from src.common import (
    load_yaml,
    read_json,
    require_clean_source,
    source_identity,
    utc_now,
    write_json,
)
from src.dynamic_batcher import Batch, PolicyName, SchedulerConfig, VirtualTimeScheduler
from src.week03_contract import (
    BATCH_FIELDS,
    EVENT_FIELDS,
    SCHEMA_VERSION,
    CaseKey,
    case_id,
    create_run_metadata,
    expand_matrix,
    trace_key,
    validate_case_artifacts,
    validate_run_metadata,
)
from src.week01_contract import collect_runtime_identity, validate_model_snapshot
from src.workload import (
    TraceRequest,
    generate_poisson_trace,
    read_trace,
    trace_fingerprint,
    write_trace,
)


@dataclass(frozen=True, slots=True)
class BackendTiming:
    service_ns: int
    first_token_ns: int
    failure_reason: str = ""

    def __post_init__(self) -> None:
        if self.service_ns < 0 or self.first_token_ns < 0:
            raise ValueError("backend timings must be nonnegative")
        if self.first_token_ns > self.service_ns:
            raise ValueError("first-token offset cannot exceed service time")


class BatchBackend(Protocol):
    name: str

    def execute(self, batch: Batch) -> BackendTiming: ...


class FakeBatchBackend:
    """Deterministic CPU-only backend; it never sleeps."""

    name = "fake"

    def __init__(
        self,
        service_ms_by_batch_size: Mapping[int | str, float],
        *,
        first_token_fraction: float = 0.25,
        fail_batch_ids: Sequence[str] = (),
    ):
        self._service_ns = {
            int(size): round(float(milliseconds) * 1_000_000)
            for size, milliseconds in service_ms_by_batch_size.items()
        }
        if not self._service_ns or any(
            size <= 0 or duration < 0 for size, duration in self._service_ns.items()
        ):
            raise ValueError("fake service times need positive sizes and nonnegative values")
        if not math.isfinite(first_token_fraction) or not 0 <= first_token_fraction <= 1:
            raise ValueError("first_token_fraction must be between zero and one")
        self._first_token_fraction = first_token_fraction
        self._fail_batch_ids = set(fail_batch_ids)

    def execute(self, batch: Batch) -> BackendTiming:
        size = len(batch.requests)
        if size in self._service_ns:
            service_ns = self._service_ns[size]
        else:
            larger = sorted(candidate for candidate in self._service_ns if candidate >= size)
            selected = larger[0] if larger else max(self._service_ns)
            service_ns = self._service_ns[selected]
        failure = "injected_fake_backend_failure" if batch.batch_id in self._fail_batch_ids else ""
        return BackendTiming(
            service_ns=service_ns,
            first_token_ns=round(service_ns * self._first_token_fraction),
            failure_reason=failure,
        )


class HFBatchBackend:
    """Lazy, single-worker Hugging Face backend for the real-time runner."""

    name = "hf"

    def __init__(self, config: Mapping[str, object]):
        from src.hf_batch_backend import (
            capture_model_memory_baseline,
            load_hf_batch_model,
            prepare_trace_batch_tensors,
            run_batched_greedy_generation,
        )

        model = _mapping(config["model"], "model")
        workload = _mapping(config["workload"], "workload")
        self._prompt = str(workload.get("prompt", "Explain dynamic batching."))
        self._padding_side = str(workload.get("padding_side", "left"))
        self._prepare = prepare_trace_batch_tensors
        self._run = run_batched_greedy_generation
        output = _mapping(config["output"], "output")
        snapshot = read_json(str(output["model_snapshot"]))
        validate_model_snapshot(config, snapshot)
        self._tokenizer, self._model, _ = load_hf_batch_model(
            str(model["id"]),
            str(model["revision"]),
            str(model["dtype"]),
            local_files_only=bool(model.get("local_files_only", True)),
            model_path=str(snapshot["snapshot_path"]),
        )
        self._baseline = capture_model_memory_baseline()

    def execute(self, batch: Batch) -> BackendTiming:
        # 服务耗时从 CPU 分词/预处理前开始：单个 worker 在这些阶段同样被占用。
        # Week 2 分开统计预处理、H2D 和 CUDA 耗时；Week 3 将其全部计入服务边界。
        started_ns = time.monotonic_ns()
        prompts = {item.request.prompt_tokens for item in batch.requests}
        outputs = {item.request.output_tokens for item in batch.requests}
        if len(prompts) != 1 or len(outputs) != 1:
            raise RuntimeError("the Week 3 HF hook currently requires one fixed request shape")
        output_tokens = next(iter(outputs))
        trace_requests = [item.request for item in batch.requests]
        host_ids, host_mask, preprocessing_ms = self._prepare(
            self._tokenizer,
            trace_requests,
            self._prompt,
            padding_side=self._padding_side,
        )
        result, _ = self._run(
            self._model,
            host_ids,
            host_mask,
            output_tokens=output_tokens,
            preprocessing_ms=preprocessing_ms,
            baseline=self._baseline,
            theoretical_kv_cache_bytes=0,
        )
        finished_ns = time.monotonic_ns()
        service_ns = max(0, finished_ns - started_ns)
        first_token_ns = round(result.e2e_ttft_ms * 1_000_000)
        if first_token_ns > service_ns:
            raise RuntimeError(
                "HF TTFT exceeded the observed preprocessing-to-finish boundary"
            )
        return BackendTiming(
            service_ns=service_ns,
            first_token_ns=first_token_ns,
        )


@dataclass(slots=True)
class RequestState:
    request: TraceRequest
    observed_arrival_ns: int
    admitted_ns: int | None = None
    dispatch_ns: int | None = None
    gpu_start_ns: int | None = None
    first_token_ns: int | None = None
    finished_ns: int | None = None
    terminal_ns: int | None = None
    status: str = ""
    batch_id: str = ""
    queue_depth_at_admission: int = 0
    failure_reason: str = ""


@dataclass(slots=True)
class ActiveBatch:
    batch: Batch
    start_ns: int
    timing: BackendTiming
    queue_depth_at_dispatch: int

    @property
    def finish_ns(self) -> int:
        return self.start_ns + self.timing.service_ns


@dataclass(frozen=True, slots=True)
class _ArrivalMessage:
    request: TraceRequest
    observed_ns: int
    deadline_ns: int


@dataclass(frozen=True, slots=True)
class _ArrivalGroup:
    messages: tuple[_ArrivalMessage, ...]


@dataclass(frozen=True, slots=True)
class _WorkerItem:
    batch: Batch
    queue_depth_at_dispatch: int


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _blank(value: object | None) -> object:
    return "" if value is None else value


def _duration(end: int | None, start: int | None) -> object:
    return "" if end is None or start is None else end - start


def _case_common(
    metadata: Mapping[str, object], case: CaseKey, trace_id: str, arrival_rate_rps: float
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": metadata["run_id"],
        "metadata_fingerprint": metadata["metadata_fingerprint"],
        "config_fingerprint": metadata["config_fingerprint"],
        "calibration_id": metadata["calibration_id"],
        "case_id": case_id(case),
        "trace_id": trace_id,
        "profile": case.profile,
        "policy": case.policy,
        "offered_load_ratio": case.offered_load_ratio,
        "arrival_rate_rps": f"{arrival_rate_rps:.9f}",
        "repeat": case.repeat,
        "max_batch_size": case.max_batch_size,
        "delay_ms": case.delay_ms,
    }


def _waiting_count(scheduler: VirtualTimeScheduler, pending: deque[tuple[Batch, int]]) -> int:
    return scheduler.queue_depth + sum(len(batch.requests) for batch, _ in pending)


def _scheduler_config(case: CaseKey, config: Mapping[str, object]) -> SchedulerConfig:
    scheduler = _mapping(config["scheduler"], "scheduler")
    delay_ns = case.delay_ms * 1_000_000
    return SchedulerConfig(
        policy=PolicyName(case.policy),
        max_batch_size=case.max_batch_size,
        window_ns=delay_ns if case.policy == "fixed_window" else 0,
        max_wait_ns=delay_ns if case.policy == "size_or_time" else 0,
        # Total waiting capacity is enforced across queued and formed batches.
        queue_capacity=int(scheduler["queue_capacity"]),
        admission_timeout_ns=round(float(scheduler["admission_timeout_ms"]) * 1_000_000),
    )


def _online_rows(
    *,
    trace: Sequence[TraceRequest],
    states: Mapping[str, RequestState],
    completed_batches: Sequence[ActiveBatch],
    case: CaseKey,
    metadata: Mapping[str, object],
    arrival_rate_rps: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    trace_id = trace_fingerprint(trace)
    common = _case_common(metadata, case, trace_id, arrival_rate_rps)
    event_rows: list[dict[str, object]] = []
    for request in trace:
        state = states[request.request_id]
        if not state.status or state.terminal_ns is None:
            raise RuntimeError(f"request did not reach a terminal state: {request.request_id}")
        event_rows.append(
            {
                **common,
                "request_id": request.request_id,
                "ordinal": request.ordinal,
                "measurement": str(request.measurement).lower(),
                "scheduled_arrival_ns": request.scheduled_arrival_ns,
                "observed_arrival_ns": state.observed_arrival_ns,
                "admitted_ns": _blank(state.admitted_ns),
                "dispatch_ns": _blank(state.dispatch_ns),
                "gpu_start_ns": _blank(state.gpu_start_ns),
                "first_token_ns": _blank(state.first_token_ns),
                "finished_ns": _blank(state.finished_ns),
                "terminal_ns": state.terminal_ns,
                "status": state.status,
                "batch_id": state.batch_id,
                "prompt_tokens": request.prompt_tokens,
                "output_tokens": request.output_tokens,
                "queue_depth_at_admission": state.queue_depth_at_admission,
                "failure_reason": state.failure_reason,
                "arrival_lag_ns": (
                    state.observed_arrival_ns - request.scheduled_arrival_ns
                ),
                "admission_wait_ns": _duration(
                    state.admitted_ns, state.observed_arrival_ns
                ),
                "queueing_delay_ns": _duration(
                    state.dispatch_ns, state.admitted_ns
                ),
                "worker_wait_ns": _duration(
                    state.gpu_start_ns, state.dispatch_ns
                ),
                "service_time_ns": _duration(
                    state.finished_ns, state.gpu_start_ns
                ),
                "ttft_ns": _duration(state.first_token_ns, state.admitted_ns),
                "e2e_latency_ns": _duration(state.finished_ns, state.admitted_ns),
            }
        )

    batch_rows: list[dict[str, object]] = []
    for completed in completed_batches:
        batch = completed.batch
        failure = completed.timing.failure_reason
        batch_rows.append(
            {
                **common,
                "batch_id": batch.batch_id,
                "trigger": batch.trigger,
                "request_ids_json": json.dumps(
                    list(batch.request_ids), separators=(",", ":")
                ),
                "request_count": len(batch.requests),
                "dispatch_ns": batch.dispatch_ns,
                "gpu_start_ns": completed.start_ns,
                "first_token_ns": (
                    ""
                    if failure
                    else completed.start_ns + completed.timing.first_token_ns
                ),
                "finished_ns": completed.finish_ns,
                "service_time_ns": completed.timing.service_ns,
                "fill_ratio": f"{len(batch.requests) / case.max_batch_size:.12f}",
                "queue_depth_at_dispatch": completed.queue_depth_at_dispatch,
                "status": "failed" if failure else "completed",
                "failure_reason": failure,
            }
        )
    validate_case_artifacts(
        event_rows,
        batch_rows,
        expected_run_id=str(metadata["run_id"]),
        expected_case_id=case_id(case),
        expected_metadata=metadata,
        expected_case=case,
        expected_trace_id=trace_id,
    )
    return event_rows, batch_rows


def run_online_case(
    *,
    trace: Sequence[TraceRequest],
    case: CaseKey,
    config: Mapping[str, object],
    metadata: Mapping[str, object],
    backend: BatchBackend,
    arrival_rate_rps: float,
    clock: object = time,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Replay arrivals on a monotonic clock through one bounded HF worker.

    The producer remains live while model execution blocks the worker thread. All
    persisted timestamps are offsets from one process-local monotonic origin.
    """

    if backend.name != "hf":
        raise ValueError("run_online_case is reserved for the real HF backend")
    scheduler = VirtualTimeScheduler(_scheduler_config(case, config))
    capacity = scheduler.config.queue_capacity
    timeout_ns = scheduler.config.admission_timeout_ns
    arrivals: queue.Queue[object] = queue.Queue()
    # One handoff slot models one worker ownership token; formed batches never
    # accumulate behind the GPU outside the bounded request queue.
    work: queue.Queue[object] = queue.Queue(maxsize=1)
    completions: queue.Queue[ActiveBatch] = queue.Queue()
    arrival_done = object()
    worker_wakeup = object()
    worker_done = object()
    monotonic_ns = getattr(clock, "monotonic_ns")
    sleep = getattr(clock, "sleep")
    origin_ns = int(monotonic_ns())
    states = {
        request.request_id: RequestState(request, request.scheduled_arrival_ns)
        for request in trace
    }
    producer_errors: list[BaseException] = []

    def relative_now() -> int:
        return max(0, int(monotonic_ns()) - origin_ns)

    def produce() -> None:
        try:
            arrival_index = 0
            while arrival_index < len(trace):
                scheduled_ns = trace[arrival_index].scheduled_arrival_ns
                target_ns = origin_ns + scheduled_ns
                while True:
                    remaining_ns = target_ns - int(monotonic_ns())
                    if remaining_ns <= 0:
                        break
                    sleep(remaining_ns / 1_000_000_000)
                group: list[_ArrivalMessage] = []
                while (
                    arrival_index < len(trace)
                    and trace[arrival_index].scheduled_arrival_ns == scheduled_ns
                ):
                    request = trace[arrival_index]
                    observed_ns = relative_now()
                    group.append(
                        _ArrivalMessage(
                            request=request,
                            observed_ns=observed_ns,
                            deadline_ns=observed_ns + timeout_ns,
                        )
                    )
                    arrival_index += 1
                arrivals.put(_ArrivalGroup(tuple(group)))
        except BaseException as error:
            producer_errors.append(error)
        finally:
            arrivals.put(arrival_done)

    def serve() -> None:
        while True:
            item = work.get()
            try:
                if item is worker_done:
                    return
                assert isinstance(item, _WorkerItem)
                start_ns = relative_now()
                try:
                    measured = backend.execute(item.batch)
                    finish_ns = relative_now()
                    service_ns = max(0, finish_ns - start_ns)
                    if measured.first_token_ns > service_ns:
                        raise RuntimeError(
                            "HF TTFT exceeded the observed preprocessing-to-finish boundary"
                        )
                    timing = BackendTiming(
                        service_ns=service_ns,
                        first_token_ns=measured.first_token_ns,
                        failure_reason=measured.failure_reason,
                    )
                except Exception as error:
                    finish_ns = relative_now()
                    timing = BackendTiming(
                        max(0, finish_ns - start_ns),
                        0,
                        f"{type(error).__name__}: {error}",
                    )
                completions.put(
                    ActiveBatch(
                        item.batch, start_ns, timing, item.queue_depth_at_dispatch
                    )
                )
                arrivals.put(worker_wakeup)
            finally:
                work.task_done()

    producer_thread = threading.Thread(
        target=produce, name=f"week03-producer-{case_id(case)}", daemon=True
    )
    worker_thread = threading.Thread(
        target=serve, name=f"week03-worker-{case_id(case)}", daemon=True
    )
    producer_thread.start()
    worker_thread.start()
    producer_finished = False
    in_flight = False
    worker_became_available = False
    processed_arrivals = 0
    pending_admissions: deque[_ArrivalMessage] = deque()
    completed_batches: list[ActiveBatch] = []

    def collect_completions() -> None:
        nonlocal in_flight, worker_became_available
        while True:
            try:
                completed = completions.get_nowait()
            except queue.Empty:
                return
            if in_flight:
                scheduler.advance_to(relative_now(), dispatch_slots=0)
            completed_batches.append(completed)
            in_flight = False
            worker_became_available = True
            failure = completed.timing.failure_reason
            for queued in completed.batch.requests:
                state = states[queued.request.request_id]
                state.gpu_start_ns = completed.start_ns
                state.terminal_ns = completed.finish_ns
                if failure:
                    state.status = "failed"
                    state.failure_reason = failure
                else:
                    state.first_token_ns = (
                        completed.start_ns + completed.timing.first_token_ns
                    )
                    state.finished_ns = completed.finish_ns
                    state.status = "completed"

    def dispatch(now_ns: int, *, closing: bool = False) -> None:
        nonlocal in_flight, worker_became_available
        if in_flight:
            scheduler.advance_to(now_ns, dispatch_slots=0)
            return
        batches = (
            scheduler.close(now_ns, dispatch_slots=1)
            if closing
            else scheduler.advance_to(now_ns, dispatch_slots=1)
        )
        for batch in batches:
            handoff_ns = relative_now()
            trigger = batch.trigger
            if batch.dispatch_ns < handoff_ns:
                oldest_admitted_ns = batch.requests[0].admitted_ns
                if trigger == "timeout" and now_ns > (
                    oldest_admitted_ns + scheduler.config.max_wait_ns
                ):
                    trigger = "worker_became_available_timeout"
                elif (
                    trigger == "size"
                    and worker_became_available
                    and now_ns > batch.dispatch_ns
                ):
                    trigger = "worker_became_available_size"
                batch = Batch(batch.batch_id, handoff_ns, batch.requests, trigger)
            depth = scheduler.queue_depth
            for queued in batch.requests:
                state = states[queued.request.request_id]
                state.dispatch_ns = batch.dispatch_ns
                state.batch_id = batch.batch_id
            work.put(_WorkerItem(batch, depth))
            in_flight = True
            worker_became_available = False

    def record_arrival_group(group: _ArrivalGroup) -> None:
        nonlocal processed_arrivals
        if not group.messages:
            raise RuntimeError("online producer published an empty arrival group")
        scheduled_ns = group.messages[0].request.scheduled_arrival_ns
        for message in group.messages:
            if message.request.scheduled_arrival_ns != scheduled_ns:
                raise RuntimeError("online producer mixed arrival timestamps in one group")
            if processed_arrivals >= len(trace):
                raise RuntimeError("online producer published too many arrivals")
            expected_request = trace[processed_arrivals]
            if message.request.request_id != expected_request.request_id:
                raise RuntimeError("online producer did not preserve trace order")
            processed_arrivals += 1
            state = states[message.request.request_id]
            state.observed_arrival_ns = message.observed_ns
            pending_admissions.append(message)

    def handle_notification(message: object) -> None:
        nonlocal producer_finished
        if message is arrival_done:
            producer_finished = True
        elif message is worker_wakeup:
            collect_completions()
        else:
            assert isinstance(message, _ArrivalGroup)
            record_arrival_group(message)

    def settle_admissions(*, expire_full_queue: bool) -> None:
        """Resolve FIFO waiters without making arrival production wait."""

        while pending_admissions:
            message = pending_admissions[0]
            decision_ns = relative_now()
            if scheduler.queue_depth < capacity and decision_ns <= message.deadline_ns:
                pending_admissions.popleft()
                decision = scheduler.submit(
                    message.request,
                    decision_ns,
                    observed_arrival_ns=message.observed_ns,
                )
                if not decision.accepted:
                    raise RuntimeError(
                        "bounded admission and scheduler capacity diverged"
                    )
                state = states[message.request.request_id]
                state.admitted_ns = decision_ns
                state.queue_depth_at_admission = scheduler.queue_depth
                continue
            if decision_ns > message.deadline_ns or (
                expire_full_queue and decision_ns >= message.deadline_ns
            ):
                pending_admissions.popleft()
                state = states[message.request.request_id]
                state.status = "rejected"
                state.terminal_ns = decision_ns
                state.failure_reason = "queue_capacity_timeout"
                continue
            return

    def unpublished_arrival_is_due() -> bool:
        return (
            processed_arrivals < len(trace)
            and trace[processed_arrivals].scheduled_arrival_ns <= relative_now()
        )

    try:
        while not producer_finished or pending_admissions:
            collect_completions()
            while True:
                try:
                    message = arrivals.get_nowait()
                except queue.Empty:
                    break
                handle_notification(message)

            # A due group is published atomically. Wait for it before any flush so
            # all equal-offset arrivals retain arrival-before-flush precedence.
            unpublished_arrival_due = unpublished_arrival_is_due()
            if not unpublished_arrival_due:
                settle_admissions(expire_full_queue=False)
                # Recheck immediately before advancing the scheduler: admission
                # bookkeeping itself may cross the next trace timestamp.
                unpublished_arrival_due = unpublished_arrival_is_due()
                if not unpublished_arrival_due:
                    dispatch(relative_now())
                    # Dispatch releases request capacity. Give each waiter its own
                    # deadline opportunity before expiring a still-full FIFO head.
                    settle_admissions(expire_full_queue=True)

            if producer_finished and not pending_admissions:
                break

            now_ns = relative_now()
            wakeups: list[int] = []
            if not in_flight:
                scheduler_wakeup = scheduler.next_wakeup_ns()
                if scheduler_wakeup is not None:
                    wakeups.append(scheduler_wakeup)
            if pending_admissions:
                wakeups.append(pending_admissions[0].deadline_ns)
            timeout_seconds = 0.1
            if wakeups and not unpublished_arrival_due:
                wakeup = min(wakeups)
                timeout_seconds = min(
                    timeout_seconds, max(0.0, (wakeup - now_ns) / 1_000_000_000)
                )
            try:
                message = arrivals.get(timeout=timeout_seconds)
            except queue.Empty:
                continue
            handle_notification(message)

        producer_thread.join()
        if producer_errors:
            raise RuntimeError("Week 3 online producer failed") from producer_errors[0]
        while in_flight or not scheduler.closed:
            collect_completions()
            dispatch(relative_now(), closing=True)
            if in_flight:
                try:
                    completed = completions.get(timeout=0.1)
                except queue.Empty:
                    continue
                completions.put(completed)
        work.join()
        collect_completions()
    finally:
        producer_thread.join(timeout=1)
        if worker_thread.is_alive():
            work.put(worker_done)
            work.join()
            worker_thread.join()
    return _online_rows(
        trace=trace,
        states=states,
        completed_batches=completed_batches,
        case=case,
        metadata=metadata,
        arrival_rate_rps=arrival_rate_rps,
    )


def simulate_case(
    *,
    trace: Sequence[TraceRequest],
    case: CaseKey,
    config: Mapping[str, object],
    metadata: Mapping[str, object],
    backend: BatchBackend,
    arrival_rate_rps: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Run one single-worker case entirely by deterministic event simulation."""
    if backend.name != "fake":
        raise ValueError(
            "simulate_case is simulation-only; use run_online_case for HF evidence"
        )

    scheduler = VirtualTimeScheduler(_scheduler_config(case, config))
    capacity = scheduler.config.queue_capacity
    admission_timeout_ns = scheduler.config.admission_timeout_ns
    trace_id = trace_fingerprint(trace)
    states = {
        request.request_id: RequestState(request, request.scheduled_arrival_ns)
        for request in trace
    }
    pending_admissions: deque[tuple[TraceRequest, int]] = deque()
    pending_batches: deque[tuple[Batch, int]] = deque()
    completed_batches: list[ActiveBatch] = []
    active: ActiveBatch | None = None
    arrival_index = 0
    now_ns = 0

    def admit_waiters() -> bool:
        changed = False
        while pending_admissions and _waiting_count(scheduler, pending_batches) < capacity:
            request, deadline = pending_admissions[0]
            if deadline < now_ns:
                break
            pending_admissions.popleft()
            state = states[request.request_id]
            decision = scheduler.submit(
                request, now_ns, observed_arrival_ns=state.observed_arrival_ns
            )
            if not decision.accepted:
                raise RuntimeError("external admission accounting diverged from scheduler")
            state.admitted_ns = now_ns
            state.queue_depth_at_admission = _waiting_count(scheduler, pending_batches)
            changed = True
        return changed

    def start_worker() -> bool:
        nonlocal active
        if active is not None or not pending_batches:
            return False
        batch, depth = pending_batches.popleft()
        try:
            timing = backend.execute(batch)
        except Exception as error:  # GPU/backend failures are terminal evidence.
            timing = BackendTiming(0, 0, f"{type(error).__name__}: {error}")
        active = ActiveBatch(batch, now_ns, timing, depth)
        for item in batch.requests:
            state = states[item.request.request_id]
            state.gpu_start_ns = now_ns
        return True

    def finish_worker() -> bool:
        nonlocal active
        if active is None or active.finish_ns != now_ns:
            return False
        current = active
        active = None
        failure = current.timing.failure_reason
        for item in current.batch.requests:
            state = states[item.request.request_id]
            state.terminal_ns = now_ns
            if failure:
                state.status = "failed"
                state.failure_reason = failure
            else:
                state.first_token_ns = current.start_ns + current.timing.first_token_ns
                state.finished_ns = now_ns
                state.status = "completed"
        completed_batches.append(current)
        return True

    def dispatch_ready() -> bool:
        batches = scheduler.advance_to(now_ns)
        for batch in batches:
            depth = _waiting_count(scheduler, pending_batches)
            for item in batch.requests:
                state = states[item.request.request_id]
                state.dispatch_ns = batch.dispatch_ns
                state.batch_id = batch.batch_id
            pending_batches.append((batch, depth))
        return bool(batches)

    while True:
        next_values: list[int] = []
        if arrival_index < len(trace):
            next_values.append(trace[arrival_index].scheduled_arrival_ns)
        if active is not None:
            next_values.append(active.finish_ns)
        wakeup = scheduler.next_wakeup_ns()
        if wakeup is not None:
            next_values.append(wakeup)
        if pending_admissions:
            next_values.append(pending_admissions[0][1])
        if not next_values:
            break
        next_ns = min(value for value in next_values if value >= now_ns)
        now_ns = next_ns
        finish_worker()
        start_worker()
        admit_waiters()

        while (
            arrival_index < len(trace)
            and trace[arrival_index].scheduled_arrival_ns == now_ns
        ):
            request = trace[arrival_index]
            arrival_index += 1
            if _waiting_count(scheduler, pending_batches) < capacity:
                decision = scheduler.submit(request, now_ns, observed_arrival_ns=now_ns)
                if not decision.accepted:
                    raise RuntimeError("scheduler rejected despite external free capacity")
                state = states[request.request_id]
                state.admitted_ns = now_ns
                state.queue_depth_at_admission = _waiting_count(scheduler, pending_batches)
            else:
                pending_admissions.append((request, now_ns + admission_timeout_ns))

        # Arrival-before-flush gives exact-boundary arrivals deterministic inclusion.
        dispatch_ready()
        if start_worker():
            admit_waiters()
            dispatch_ready()

        # A capacity slot becoming free exactly at a deadline wins. Reject only
        # after every possible admission/dispatch transition at this timestamp.
        while pending_admissions and pending_admissions[0][1] <= now_ns:
            request, _ = pending_admissions.popleft()
            state = states[request.request_id]
            state.status = "rejected"
            state.terminal_ns = now_ns
            state.failure_reason = "queue_capacity_timeout"

    # End-of-trace closes batch formation but the single worker drains all work.
    final_batches = scheduler.close(now_ns)
    for batch in final_batches:
        for item in batch.requests:
            state = states[item.request.request_id]
            state.dispatch_ns = batch.dispatch_ns
            state.batch_id = batch.batch_id
        pending_batches.append((batch, _waiting_count(scheduler, pending_batches)))
    while active is not None or pending_batches:
        if active is None:
            start_worker()
        assert active is not None
        now_ns = active.finish_ns
        finish_worker()

    common = _case_common(metadata, case, trace_id, arrival_rate_rps)
    event_rows: list[dict[str, object]] = []
    for request in trace:
        state = states[request.request_id]
        if not state.status or state.terminal_ns is None:
            raise RuntimeError(f"request did not reach a terminal state: {request.request_id}")
        event_rows.append(
            {
                **common,
                "request_id": request.request_id,
                "ordinal": request.ordinal,
                "measurement": str(request.measurement).lower(),
                "scheduled_arrival_ns": request.scheduled_arrival_ns,
                "observed_arrival_ns": state.observed_arrival_ns,
                "admitted_ns": _blank(state.admitted_ns),
                "dispatch_ns": _blank(state.dispatch_ns),
                "gpu_start_ns": _blank(state.gpu_start_ns),
                "first_token_ns": _blank(state.first_token_ns),
                "finished_ns": _blank(state.finished_ns),
                "terminal_ns": state.terminal_ns,
                "status": state.status,
                "batch_id": state.batch_id,
                "prompt_tokens": request.prompt_tokens,
                "output_tokens": request.output_tokens,
                "queue_depth_at_admission": state.queue_depth_at_admission,
                "failure_reason": state.failure_reason,
                "arrival_lag_ns": (
                    state.observed_arrival_ns - request.scheduled_arrival_ns
                ),
                "admission_wait_ns": _duration(state.admitted_ns, state.observed_arrival_ns),
                "queueing_delay_ns": _duration(state.dispatch_ns, state.admitted_ns),
                "worker_wait_ns": _duration(state.gpu_start_ns, state.dispatch_ns),
                "service_time_ns": _duration(state.finished_ns, state.gpu_start_ns),
                "ttft_ns": _duration(state.first_token_ns, state.admitted_ns),
                "e2e_latency_ns": _duration(state.finished_ns, state.admitted_ns),
            }
        )
    batch_rows: list[dict[str, object]] = []
    for completed in completed_batches:
        batch = completed.batch
        failure = completed.timing.failure_reason
        batch_rows.append(
            {
                **common,
                "batch_id": batch.batch_id,
                "trigger": batch.trigger,
                "request_ids_json": json.dumps(
                    list(batch.request_ids), separators=(",", ":")
                ),
                "request_count": len(batch.requests),
                "dispatch_ns": batch.dispatch_ns,
                "gpu_start_ns": completed.start_ns,
                "first_token_ns": (
                    ""
                    if failure
                    else completed.start_ns + completed.timing.first_token_ns
                ),
                "finished_ns": completed.finish_ns,
                "service_time_ns": completed.timing.service_ns,
                "fill_ratio": f"{len(batch.requests) / case.max_batch_size:.12f}",
                "queue_depth_at_dispatch": completed.queue_depth_at_dispatch,
                "status": "failed" if failure else "completed",
                "failure_reason": failure,
            }
        )
    validate_case_artifacts(
        event_rows,
        batch_rows,
        expected_run_id=str(metadata["run_id"]),
        expected_case_id=case_id(case),
        expected_metadata=metadata,
        expected_case=case,
        expected_trace_id=trace_id,
    )
    return event_rows, batch_rows


def _write_csv_atomic(
    path: str | Path, rows: Sequence[Mapping[str, object]], fields: Sequence[str]
) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _read_csv_exact(path: str | Path, fields: Sequence[str]) -> list[dict[str, str]]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(fields):
            raise ValueError(
                f"CSV header mismatch for {path}: expected {list(fields)}, got {reader.fieldnames}"
            )
        return list(reader)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _case_is_complete(
    directory: Path, metadata: Mapping[str, object], expected_case: CaseKey
) -> bool:
    marker_path = directory / "complete.json"
    events_path = directory / "events.csv"
    batches_path = directory / "batches.csv"
    if not all(path.is_file() for path in (marker_path, events_path, batches_path)):
        return False
    try:
        marker = read_json(marker_path)
        if (
            marker.get("schema_version") != SCHEMA_VERSION
            or marker.get("run_id") != metadata["run_id"]
            or marker.get("case_id") != case_id(expected_case)
            or marker.get("metadata_fingerprint") != metadata["metadata_fingerprint"]
            or marker.get("config_fingerprint") != metadata["config_fingerprint"]
            or marker.get("calibration_id") != metadata["calibration_id"]
            or marker.get("sha256", {}).get("events.csv") != _file_sha256(events_path)
            or marker.get("sha256", {}).get("batches.csv") != _file_sha256(batches_path)
        ):
            return False
        events = _read_csv_exact(events_path, EVENT_FIELDS)
        batches = _read_csv_exact(batches_path, BATCH_FIELDS)
        marker_trace_id = str(marker.get("trace_id", ""))
        if not marker_trace_id:
            return False
        validate_case_artifacts(
            events,
            batches,
            expected_run_id=str(metadata["run_id"]),
            expected_case_id=case_id(expected_case),
            expected_metadata=metadata,
            expected_case=expected_case,
            expected_trace_id=marker_trace_id,
        )
    except (KeyError, OSError, TypeError, ValueError):
        return False
    return True


def _commit_case(
    directory: Path,
    event_rows: Sequence[Mapping[str, object]],
    batch_rows: Sequence[Mapping[str, object]],
    metadata: Mapping[str, object],
    case: CaseKey,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    events_path = directory / "events.csv"
    batches_path = directory / "batches.csv"
    _write_csv_atomic(events_path, event_rows, EVENT_FIELDS)
    _write_csv_atomic(batches_path, batch_rows, BATCH_FIELDS)
    write_json(
        directory / "complete.json",
        {
            "schema_version": SCHEMA_VERSION,
            "completed_at": utc_now(),
            "run_id": metadata["run_id"],
            "metadata_fingerprint": metadata["metadata_fingerprint"],
            "config_fingerprint": metadata["config_fingerprint"],
            "calibration_id": metadata["calibration_id"],
            "case_id": case_id(case),
            "trace_id": event_rows[0]["trace_id"] if event_rows else "",
            "event_count": len(event_rows),
            "batch_count": len(batch_rows),
            "sha256": {
                "events.csv": _file_sha256(events_path),
                "batches.csv": _file_sha256(batches_path),
            },
        },
    )


def _output_paths(config: Mapping[str, object], output_root: str | Path | None) -> dict[str, Path]:
    output = _mapping(config["output"], "output")
    root = Path(output_root or str(output.get("root", "results/week03")))
    return {
        "root": root,
        "traces": root / "raw" / "traces",
        "cases": root / "raw" / "cases",
        "events": root / "raw" / "events.csv",
        "batches": root / "raw" / "batches.csv",
        "metadata": root / "raw" / "run_metadata.json",
        "status": root / "run-status.json",
    }


def _profile_workload(
    config: Mapping[str, object], profile: str
) -> tuple[float, float, int | None]:
    workload = _mapping(config["workload"], "workload")
    matrix = _mapping(config["matrix"], "matrix")
    section = _mapping(matrix[profile], f"matrix.{profile}")
    duration = float(section.get("duration_seconds", workload["duration_seconds"]))
    warmup = float(section.get("warmup_seconds", workload["warmup_seconds"]))
    max_requests = section.get("max_requests")
    if duration <= 0 or warmup < 0 or warmup >= duration:
        raise ValueError(f"matrix.{profile} has invalid duration/warmup")
    return duration, warmup, None if max_requests is None else int(max_requests)


def _trace_path(paths: Mapping[str, Path], key: tuple[str, str, int]) -> Path:
    profile, ratio, repeat = key
    return paths["traces"] / (
        f"{profile}-load-{ratio.replace('.', 'p')}-repeat-{repeat}.csv"
    )


def _trace_seed(base_seed: int, key: tuple[str, str, int]) -> int:
    encoded = f"{base_seed}|{key[0]}|{key[1]}|{key[2]}".encode()
    return int.from_bytes(sha256(encoded).digest()[:8], "big")


def _generate_trace(
    config: Mapping[str, object], case: CaseKey, capacity_rps: float
) -> tuple[TraceRequest, ...]:
    workload = _mapping(config["workload"], "workload")
    duration, warmup, max_requests = _profile_workload(config, case.profile)
    return generate_poisson_trace(
        arrival_rate_rps=capacity_rps * case.ratio,
        duration_seconds=duration,
        warmup_ns=round(warmup * 1_000_000_000),
        seed=_trace_seed(int(workload["seed"]), trace_key(case)),
        prompt_tokens=int(workload["prompt_tokens"]),
        output_tokens=int(workload["output_tokens"]),
        max_requests=max_requests,
    )


def _make_backend(name: str, config: Mapping[str, object]) -> BatchBackend:
    if name == "fake":
        fake = _mapping(config["fake_backend"], "fake_backend")
        service = _mapping(fake["service_time_ms_by_batch_size"], "service times")
        return FakeBatchBackend(
            service, first_token_fraction=float(fake.get("first_token_fraction", 0.25))
        )
    if name == "hf":
        return HFBatchBackend(config)
    raise ValueError(f"unsupported Week 3 backend: {name}")


def _metadata_for_run(
    config: Mapping[str, object],
    paths: Mapping[str, Path],
    *,
    profile: str,
    backend: str,
) -> dict[str, object]:
    path = paths["metadata"]
    source: Mapping[str, object] | None = None
    runtime: Mapping[str, object] | None = None
    calibration: Mapping[str, object] | None = None
    if backend == "hf":
        from src.week03_calibration import load_calibration_artifact

        source = source_identity()
        require_clean_source(dict(source))
        runtime = collect_runtime_identity(config)
        calibration_path = _mapping(config["calibration"], "calibration")["artifact"]
        calibration = load_calibration_artifact(
            str(calibration_path), week3_config=config, runtime=runtime
        )
    if path.is_file():
        metadata = read_json(path)
        validate_run_metadata(
            metadata,
            config,
            profile=profile,
            backend=backend,
            source=source,
            runtime=runtime,
            calibration=calibration,
        )
        return metadata
    metadata = create_run_metadata(
        config,
        profile=profile,
        backend=backend,
        source=source,
        runtime=runtime,
        calibration=calibration,
    )
    write_json(path, metadata)
    return metadata


def _rebuild_aggregates(
    cases: Sequence[CaseKey], paths: Mapping[str, Path], metadata: Mapping[str, object]
) -> tuple[int, int, int]:
    events: list[dict[str, str]] = []
    batches: list[dict[str, str]] = []
    complete_count = 0
    for case in cases:
        directory = paths["cases"] / case_id(case)
        if not _case_is_complete(directory, metadata, case):
            continue
        complete_count += 1
        events.extend(_read_csv_exact(directory / "events.csv", EVENT_FIELDS))
        batches.extend(_read_csv_exact(directory / "batches.csv", BATCH_FIELDS))
    _write_csv_atomic(paths["events"], events, EVENT_FIELDS)
    _write_csv_atomic(paths["batches"], batches, BATCH_FIELDS)
    return complete_count, len(events), len(batches)


def run_matrix(
    config: Mapping[str, object],
    *,
    profile: str = "primary",
    backend_name: str = "fake",
    output_root: str | Path | None = None,
    max_cases: int | None = None,
) -> dict[str, object]:
    """运行或恢复请求级组批矩阵，共享到达轨迹并保存请求、批次与进度证据。"""
    cases = expand_matrix(config, profile)
    if max_cases is not None and max_cases < 0:
        raise ValueError("max_cases must be nonnegative")
    output = _mapping(config["output"], "output")
    selected_root = output_root
    # 默认将 fake 模拟和非主测试产物隔离，避免与正式 HF 证据混用。
    if selected_root is None and backend_name == "fake":
        selected_root = str(output.get("simulation_root", "results/week03-simulation"))
    if selected_root is None and profile != "primary":
        selected_root = str(output.get("smoke_root", f"results/week03-{profile}"))
    paths = _output_paths(config, selected_root)
    metadata = _metadata_for_run(
        config, paths, profile=profile, backend=backend_name
    )
    calibration = _mapping(metadata["calibration"], "metadata.calibration")
    # 用校准容量乘以 case 的负载比例，确定请求到达率，而非随意指定绝对压力。
    capacity_rps = float(calibration["capacity_rps"])
    if not math.isfinite(capacity_rps) or capacity_rps <= 0:
        raise ValueError("calibration.capacity_rps must be finite and positive")

    # 先保存并校验所有唯一 trace，再执行策略；同一 trace key 的策略使用相同到达序列。
    # 续跑时必须与当前配置生成的轨迹指纹一致，不能悄悄替换请求输入。
    traces: dict[tuple[str, str, int], tuple[TraceRequest, ...]] = {}
    for case in cases:
        key = trace_key(case)
        if key in traces:
            continue
        expected = _generate_trace(config, case, capacity_rps)
        path = _trace_path(paths, key)
        if path.is_file():
            existing = read_trace(path)
            if trace_fingerprint(existing) != trace_fingerprint(expected):
                raise ValueError(f"persisted trace differs from current config: {path}")
            traces[key] = existing
        else:
            write_trace(path, expected)
            traces[key] = expected

    backend = _make_backend(backend_name, config)
    ran = 0
    skipped = 0
    for case in cases:
        directory = paths["cases"] / case_id(case)
        # 只跳过完整且通过身份/产物校验的用例，不仅检查目录是否存在。
        if _case_is_complete(directory, metadata, case):
            skipped += 1
            continue
        if max_cases is not None and ran >= max_cases:
            break
        trace = traces[trace_key(case)]
        # fake 使用模拟时间；HF 使用真实到达与 worker 执行，不可将模拟结果视为 GPU 性能。
        runner = simulate_case if backend_name == "fake" else run_online_case
        events, batches = runner(
            trace=trace,
            case=case,
            config=config,
            metadata=metadata,
            backend=backend,
            arrival_rate_rps=capacity_rps * case.ratio,
        )
        _commit_case(directory, events, batches, metadata, case)
        ran += 1
        print(f"completed {case_id(case)}: requests={len(events)}, batches={len(batches)}")

    # 从已提交的用例产物重建汇总，避免中断造成总表与单用例记录不一致。
    complete, event_count, batch_count = _rebuild_aggregates(cases, paths, metadata)
    status = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "status": "benchmark_complete" if complete == len(cases) else "partial",
        "run_id": metadata["run_id"],
        "profile": profile,
        "backend": backend_name,
        # HF 结果仍只是正式候选证据；矩阵完成不等于报告及最终验收已完成。
        "evidence_class": (
            "official_candidate" if backend_name == "hf" else "simulation_only"
        ),
        "output_root": str(paths["root"]),
        "completed_cases": complete,
        "expected_cases": len(cases),
        "event_count": event_count,
        "batch_count": batch_count,
    }
    write_json(paths["status"], status)
    return {**status, "ran_cases": ran, "skipped_cases": skipped}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run or resume the Week 3 request-level batching matrix."
    )
    parser.add_argument("--config", default="configs/week03.yaml")
    parser.add_argument("--profile", default="primary")
    parser.add_argument("--backend", choices=("fake", "hf"), default="fake")
    parser.add_argument("--output-root")
    parser.add_argument("--max-cases", type=int)
    return parser.parse_args()


def main() -> None:
    """解析运行选项并交给 run_matrix；默认 fake 后端不执行 GPU 推理。"""
    args = parse_args()
    config = load_yaml(args.config)
    summary = run_matrix(
        config,
        profile=args.profile,
        backend_name=args.backend,
        output_root=args.output_root,
        max_cases=args.max_cases,
    )
    print(
        f"Week 3 {summary['status']}: "
        f"cases={summary['completed_cases']}/{summary['expected_cases']}, "
        f"events={summary['event_count']}, batches={summary['batch_count']}"
    )


if __name__ == "__main__":
    main()
