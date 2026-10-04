"""Replay an immutable Week 3 arrival trace against the Week 4 server.

The trace is the experiment input.  This client never redraws arrivals or request
shapes, and it keeps warm-up rows in the request-level evidence while excluding
them from measurement-window aggregates.  Transformer and HTTP imports are lazy
so planning and unit tests remain CPU-only.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from src.common import load_yaml, read_json, utc_now, write_json
from src.vllm_contract import validate_config
from src.week03_contract import (
    EVENT_FIELDS,
    case_id as week03_case_id,
    expand_matrix as expand_week03_matrix,
    validate_event_rows,
    validate_run_metadata as validate_week03_run_metadata,
)
from src.week04_contract import (
    artifact_identity,
    load_run_metadata,
    server_attempt_identity,
    sha256_file,
    validate_artifact_identity,
)
from src.workload import (
    TRACE_SCHEMA_VERSION,
    TraceRequest,
    read_trace,
    trace_fingerprint,
)

SCHEMA_VERSION = 1
NANOSECONDS_PER_SECOND = 1_000_000_000


class TokenizerLike(Protocol):
    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]: ...


StreamFactory = Callable[
    [str, Mapping[str, object], float, str | None], Iterable[object]
]


def exponential_arrival_offsets(
    count: int, request_rate: float, seed: int
) -> list[float]:
    """Legacy planning helper; formal Week 4 comparison runs use ``read_trace``."""

    if count < 1 or not math.isfinite(request_rate) or request_rate <= 0:
        raise ValueError("count and request_rate must be positive")
    rng = random.Random(seed)
    offsets = [0.0]
    for _ in range(1, count):
        offsets.append(offsets[-1] + rng.expovariate(request_rate))
    return offsets


def _positive_float(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite positive number")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite positive number") from error
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return result


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if result < 1 or result != value:
        raise ValueError(f"{name} must be a positive integer")
    return result


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a non-negative integer") from error
    if result < 0 or result != value:
        raise ValueError(f"{name} must be a non-negative integer")
    return result


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _canonical_uuid(value: object, name: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{name} must be a canonical UUID") from error
    canonical = str(parsed)
    if str(value) != canonical:
        raise ValueError(f"{name} must be a canonical UUID")
    return canonical


def load_ready_server_identity(
    config: Mapping[str, object], metadata: Mapping[str, object]
) -> dict[str, str]:
    validated = validate_config(config)
    server = _mapping(validated["server"], "server")
    path = Path(str(server["pid_metadata_path"]))
    if not path.is_file():
        raise ValueError(f"server ownership metadata is missing: {path}")
    payload = read_json(path)
    if not isinstance(payload, Mapping):
        raise ValueError("server ownership metadata must be a JSON object")
    validate_artifact_identity(payload, metadata, "server ownership metadata")
    if payload.get("state") != "ready":
        raise ValueError("server ownership metadata is not ready")
    return server_attempt_identity(payload, "server ownership metadata")


def load_ready_server_instance_id(
    config: Mapping[str, object], metadata: Mapping[str, object]
) -> str:
    """Compatibility accessor for callers that only display the logical ID."""

    return load_ready_server_identity(config, metadata)["server_instance_id"]


def _balanced_shape(config: Mapping[str, object]) -> tuple[int, int]:
    validated = validate_config(config)
    benchmark = _mapping(validated["benchmark"], "benchmark")
    workloads = _mapping(benchmark["workloads"], "benchmark.workloads")
    balanced = _mapping(workloads["balanced"], "benchmark.workloads.balanced")
    return int(balanced["prompt_tokens"]), int(balanced["output_tokens"])


def _comparison_base_prompt(config: Mapping[str, object]) -> str:
    comparison = _mapping(
        _mapping(config["benchmark"], "benchmark")["comparison"],
        "benchmark.comparison",
    )
    week03_config_path = Path(str(comparison["week03_config"]))
    week03_config = load_yaml(week03_config_path)
    workload = _mapping(week03_config.get("workload"), "Week 3 workload")
    week03_prompt = str(workload.get("prompt", "")).strip()
    configured_prompt = str(comparison.get("base_prompt", "")).strip()
    if not week03_prompt:
        raise ValueError("Week 3 comparison config has no workload prompt")
    if configured_prompt != week03_prompt:
        raise ValueError(
            "Week 4 comparison.base_prompt differs from the Week 3 workload prompt"
        )
    return configured_prompt


def _validate_balanced_trace(
    config: Mapping[str, object], trace: Sequence[TraceRequest]
) -> tuple[int, int]:
    if not trace:
        raise ValueError("Week 3 trace contains no requests")
    prompt_tokens, output_tokens = _balanced_shape(config)
    mismatches = [
        request.request_id
        for request in trace
        if (request.prompt_tokens, request.output_tokens)
        != (prompt_tokens, output_tokens)
    ]
    if mismatches:
        raise ValueError(
            "Week 3 trace does not match the Week 4 balanced request shape; "
            f"first mismatch={mismatches[0]}"
        )
    if not any(request.measurement for request in trace):
        raise ValueError("Week 3 trace has no measurement-window requests")
    return prompt_tokens, output_tokens


def _trace_request_payload(request: TraceRequest, trace_id: str) -> dict[str, object]:
    """Return every persisted semantic field from one Week 3 trace row."""

    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "trace_id": trace_id,
        "ordinal": request.ordinal,
        "request_id": request.request_id,
        "scheduled_arrival_ns": request.scheduled_arrival_ns,
        "prompt_tokens": request.prompt_tokens,
        "output_tokens": request.output_tokens,
        "workload_class": request.workload_class,
        "measurement": request.measurement,
    }


def _parse_bool(value: object, name: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise ValueError(f"{name} has an invalid boolean value: {value!r}")


def load_tokenizer(config: Mapping[str, object]) -> TokenizerLike:
    """Load the pinned tokenizer lazily, without importing Torch or CUDA."""

    from transformers import AutoTokenizer

    validated = validate_config(config)
    model = _mapping(validated["model"], "model")
    source = str(model.get("model_path") or model["id"])
    return AutoTokenizer.from_pretrained(
        source,
        revision=str(model["revision"]),
        local_files_only=bool(model.get("local_files_only", True)),
        trust_remote_code=False,
    )


def deterministic_prompt_token_ids(
    tokenizer: TokenizerLike,
    request: TraceRequest,
    base_prompt: str,
) -> list[int]:
    """Build deterministic token IDs of exactly the trace-requested length.

    Token IDs are sent directly to the completions endpoint, avoiding an unsafe
    decode/re-tokenize round trip.  This controls requested shape without claiming
    that Week 3 and Week 4 sent byte-identical prompt content.
    """

    from src.hf_batch_backend import deterministic_request_token_ids

    token_ids = deterministic_request_token_ids(
        tokenizer, request.request_id, base_prompt, request.prompt_tokens
    )
    if len(token_ids) != request.prompt_tokens:
        raise RuntimeError("exact-length prompt construction returned the wrong length")
    if any(
        isinstance(token, bool) or not isinstance(token, int) for token in token_ids
    ):
        raise TypeError("tokenizer returned a non-integer prompt token ID")
    return token_ids


def _prompt_sha256(token_ids: Sequence[int]) -> str:
    encoded = json.dumps(list(token_ids), separators=(",", ":")).encode("ascii")
    return sha256(encoded).hexdigest()


def completion_payload(
    config: Mapping[str, object],
    request: TraceRequest,
    prompt_token_ids: Sequence[int],
) -> dict[str, object]:
    validated = validate_config(config)
    model = _mapping(validated["model"], "model")
    benchmark = _mapping(validated["benchmark"], "benchmark")
    if len(prompt_token_ids) != request.prompt_tokens:
        raise ValueError("prompt token IDs do not match the trace request shape")
    if request.prompt_tokens + request.output_tokens > int(
        validated["server"]["max_model_len"]
    ):
        raise ValueError(f"request {request.request_id} exceeds server.max_model_len")
    return {
        "model": model["served_model_name"],
        "prompt": list(prompt_token_ids),
        "max_tokens": request.output_tokens,
        "temperature": 0.0,
        "seed": int(benchmark["seed"]) + request.ordinal,
        "stream": True,
        "stream_options": {"include_usage": True},
        "ignore_eos": True,
    }


def _default_stream_factory(
    url: str, payload: Mapping[str, object], timeout: float, api_key: str | None
) -> Iterable[object]:
    from src.openai_stream import stream_openai

    return stream_openai(
        url,
        payload,
        api_key=api_key,
        timeout=timeout,
        max_duration=timeout,
    )


def _base_record(
    request: TraceRequest,
    trace_id: str,
    prompt_token_ids: Sequence[int],
    *,
    origin_ns: int,
    submitted_ns: int,
    capacity_wait_ns: int,
) -> dict[str, object]:
    submitted_offset_ns = submitted_ns - origin_ns
    return {
        **_trace_request_payload(request, trace_id),
        "requested_prompt_tokens": request.prompt_tokens,
        "requested_output_tokens": request.output_tokens,
        "prompt_token_count": len(prompt_token_ids),
        "prompt_token_ids_sha256": _prompt_sha256(prompt_token_ids),
        "prompt_equivalence": "week03_identical_deterministic_token_ids",
        "submitted_ns": submitted_offset_ns,
        "submission_lag_ns": submitted_offset_ns - request.scheduled_arrival_ns,
        "observed_arrival_ns": None,
        "arrival_lag_ns": None,
        "executor_capacity_wait_ns": capacity_wait_ns,
        "request_started_ns": None,
        "executor_queue_wait_ns": None,
        "first_token_ns": None,
        "terminal_ns": None,
        "status": "error",
        "error_type": None,
        "finish_reason": None,
        "stream_event_count": 0,
        "actual_prompt_tokens": None,
        "actual_output_tokens": None,
        "prompt_shape_match": None,
        "output_shape_match": None,
        "ttft_ms": None,
        "tpot_ms": None,
        "e2e_ms": None,
        "network_e2e_ms": None,
    }


def run_one(
    config: Mapping[str, object],
    request: TraceRequest,
    prompt_token_ids: Sequence[int],
    *,
    trace_id: str,
    origin_ns: int,
    submitted_ns: int,
    capacity_wait_ns: int,
    api_key: str | None = None,
    stream_factory: StreamFactory | None = None,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> dict[str, object]:
    """Execute one prepared trace request and return sanitized terminal evidence."""

    validated = validate_config(config)
    server = _mapping(validated["server"], "server")
    smoke = _mapping(validated["smoke"], "smoke")
    host = str(server["host"])
    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    url = f"http://{rendered_host}:{server['port']}/v1/completions"
    payload = completion_payload(validated, request, prompt_token_ids)
    record = _base_record(
        request,
        trace_id,
        prompt_token_ids,
        origin_ns=origin_ns,
        submitted_ns=submitted_ns,
        capacity_wait_ns=capacity_wait_ns,
    )
    started_ns = clock_ns()
    started_offset_ns = started_ns - origin_ns
    record["request_started_ns"] = started_offset_ns
    record["observed_arrival_ns"] = started_offset_ns
    record["arrival_lag_ns"] = started_offset_ns - request.scheduled_arrival_ns
    if record["arrival_lag_ns"] < 0:
        raise RuntimeError("request worker started before its scheduled arrival")
    record["executor_queue_wait_ns"] = max(0, started_ns - submitted_ns)
    first_ns: int | None = None
    finish_reason: str | None = None
    usage: Mapping[str, Any] | None = None
    done_seen = False
    event_count = 0
    factory = stream_factory or _default_stream_factory
    try:
        for chunk in factory(url, payload, float(smoke["timeout_seconds"]), api_key):
            event_count += 1
            if getattr(chunk, "done", False):
                done_seen = True
                continue
            chunk_usage = getattr(chunk, "usage", None)
            if isinstance(chunk_usage, Mapping):
                usage = chunk_usage
            for choice in getattr(chunk, "choices", ()):
                content = getattr(choice, "content", None)
                if isinstance(content, str) and content and first_ns is None:
                    first_ns = clock_ns()
                candidate_reason = getattr(choice, "finish_reason", None)
                if candidate_reason is not None:
                    finish_reason = str(candidate_reason)
        terminal_ns = clock_ns()
        actual_prompt = (
            int(usage["prompt_tokens"])
            if usage is not None and usage.get("prompt_tokens") is not None
            else None
        )
        actual_output = (
            int(usage["completion_tokens"])
            if usage is not None and usage.get("completion_tokens") is not None
            else None
        )
        protocol_error: str | None = None
        if not done_seen:
            protocol_error = "MissingDoneEvent"
        elif first_ns is None:
            protocol_error = "MissingGeneratedContent"
        elif actual_prompt is None or actual_output is None:
            protocol_error = "MissingUsage"
        elif actual_prompt != request.prompt_tokens:
            protocol_error = "PromptTokenCountMismatch"
        elif actual_output != request.output_tokens:
            protocol_error = "OutputTokenCountMismatch"
        record.update(
            {
                "status": "completed" if protocol_error is None else "error",
                "error_type": protocol_error,
                "finish_reason": finish_reason,
                "stream_event_count": event_count,
                "actual_prompt_tokens": actual_prompt,
                "actual_output_tokens": actual_output,
                "prompt_shape_match": (
                    None
                    if actual_prompt is None
                    else actual_prompt == request.prompt_tokens
                ),
                "output_shape_match": (
                    None
                    if actual_output is None
                    else actual_output == request.output_tokens
                ),
                "first_token_ns": None if first_ns is None else first_ns - origin_ns,
                "terminal_ns": terminal_ns - origin_ns,
                "ttft_ms": (
                    None if first_ns is None else (first_ns - submitted_ns) / 1_000_000
                ),
                "tpot_ms": (
                    None
                    if first_ns is None or actual_output is None or actual_output <= 1
                    else (terminal_ns - first_ns) / 1_000_000 / (actual_output - 1)
                ),
                "e2e_ms": (terminal_ns - submitted_ns) / 1_000_000,
                "network_e2e_ms": (terminal_ns - started_ns) / 1_000_000,
            }
        )
    except TimeoutError as error:
        terminal_ns = clock_ns()
        record.update(
            {
                "status": "timeout",
                "error_type": type(error).__name__,
                "stream_event_count": event_count,
                "first_token_ns": None if first_ns is None else first_ns - origin_ns,
                "terminal_ns": terminal_ns - origin_ns,
                "ttft_ms": (
                    None if first_ns is None else (first_ns - submitted_ns) / 1_000_000
                ),
                "e2e_ms": (terminal_ns - submitted_ns) / 1_000_000,
                "network_e2e_ms": (terminal_ns - started_ns) / 1_000_000,
            }
        )
    except Exception as error:
        terminal_ns = clock_ns()
        record.update(
            {
                "status": "error",
                "error_type": type(error).__name__,
                "stream_event_count": event_count,
                "first_token_ns": None if first_ns is None else first_ns - origin_ns,
                "terminal_ns": terminal_ns - origin_ns,
                "ttft_ms": (
                    None if first_ns is None else (first_ns - submitted_ns) / 1_000_000
                ),
                "e2e_ms": (terminal_ns - submitted_ns) / 1_000_000,
                "network_e2e_ms": (terminal_ns - started_ns) / 1_000_000,
            }
        )
    return record


def _nearest_rank(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(math.ceil(fraction * len(ordered)) - 1, 0)
    return float(ordered[index])


def _status_counts(records: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts = {"requested": len(records), "success": 0, "timeout": 0, "error": 0}
    for record in records:
        status = str(record.get("status"))
        if status == "completed":
            counts["success"] += 1
        elif status == "timeout":
            counts["timeout"] += 1
        else:
            counts["error"] += 1
    return counts


def summarize_records(
    records: Sequence[Mapping[str, object]],
    *,
    max_p99_arrival_lag_ms: float,
) -> dict[str, object]:
    """Summarize only measurement-window requests while retaining warm-up totals."""

    measurement = [record for record in records if record.get("measurement") is True]
    if not measurement:
        raise ValueError("trace replay has no measurement-window records")
    completed = [
        record for record in measurement if record.get("status") == "completed"
    ]
    scheduled = [int(record["scheduled_arrival_ns"]) for record in measurement]
    terminal = [int(record["terminal_ns"]) for record in measurement]
    elapsed_ns = max(1, max(terminal) - min(scheduled))
    ttft = [
        float(record["ttft_ms"])
        for record in completed
        if record.get("ttft_ms") is not None
    ]
    tpot = [
        float(record["tpot_ms"])
        for record in completed
        if record.get("tpot_ms") is not None
    ]
    e2e = [
        float(record["e2e_ms"])
        for record in completed
        if record.get("e2e_ms") is not None
    ]
    lag = [float(record["arrival_lag_ns"]) / 1_000_000 for record in measurement]
    p99_lag = _nearest_rank(lag, 0.99)
    assert p99_lag is not None
    actual_input = sum(
        int(record.get("actual_prompt_tokens") or 0) for record in completed
    )
    actual_output = sum(
        int(record.get("actual_output_tokens") or 0) for record in completed
    )
    duration_seconds = elapsed_ns / NANOSECONDS_PER_SECOND
    return {
        "all_requests": _status_counts(records),
        "warmup_requests": _status_counts(
            [record for record in records if record.get("measurement") is False]
        ),
        "measurement_requests": _status_counts(measurement),
        "tokens": {
            "actual_input_total": actual_input,
            "actual_output_total": actual_output,
        },
        "metrics": {
            "duration_seconds": duration_seconds,
            "request_throughput": len(completed) / duration_seconds,
            "output_token_throughput": actual_output / duration_seconds,
            "p50_ttft_ms": _nearest_rank(ttft, 0.50),
            "p95_ttft_ms": _nearest_rank(ttft, 0.95),
            "p99_ttft_ms": _nearest_rank(ttft, 0.99),
            "p50_tpot_ms": _nearest_rank(tpot, 0.50),
            "p95_tpot_ms": _nearest_rank(tpot, 0.95),
            "p99_tpot_ms": _nearest_rank(tpot, 0.99),
            "p50_e2e_ms": _nearest_rank(e2e, 0.50),
            "p95_e2e_ms": _nearest_rank(e2e, 0.95),
            "p99_e2e_ms": _nearest_rank(e2e, 0.99),
            "server_queue_ms": None,
            "p50_arrival_lag_ms": _nearest_rank(lag, 0.50),
            "p95_arrival_lag_ms": _nearest_rank(lag, 0.95),
            "p99_arrival_lag_ms": p99_lag,
            "max_arrival_lag_ms": max(lag),
        },
        "arrival_lag_gate": {
            "max_p99_arrival_lag_ms": max_p99_arrival_lag_ms,
            "observed_p99_arrival_lag_ms": p99_lag,
            "passed": p99_lag <= max_p99_arrival_lag_ms,
        },
    }


def plan_trace(
    config: Mapping[str, object],
    trace_path: str | Path,
    *,
    request_rate: float,
    repeat: int,
    max_workers: int,
) -> dict[str, object]:
    validated = validate_config(config)
    rate = _positive_float(request_rate, "request_rate")
    repeat_number = _nonnegative_int(repeat, "repeat")
    workers = _positive_int(max_workers, "max_workers")
    path = Path(trace_path)
    trace = read_trace(path)
    prompt_tokens, output_tokens = _validate_balanced_trace(validated, trace)
    _comparison_base_prompt(validated)
    trace_id = trace_fingerprint(trace)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "plan",
        "trace_path": str(path),
        "trace_id": trace_id,
        "trace_file_sha256": sha256_file(path),
        "request_rate": rate,
        "repeat": repeat_number,
        "max_workers": workers,
        "request_count": len(trace),
        "warmup_request_count": sum(not item.measurement for item in trace),
        "measurement_request_count": sum(item.measurement for item in trace),
        "requested_prompt_tokens": prompt_tokens,
        "requested_output_tokens": output_tokens,
        "requests": [_trace_request_payload(request, trace_id) for request in trace],
        "equivalence_contract": {
            "same_trace_id": True,
            "same_request_ids": True,
            "same_arrival_offsets": True,
            "same_warmup_measurement_flags": True,
            "same_requested_token_shapes": True,
            "identical_prompt_token_ids": True,
            "byte_identical_prompt_claimed": True,
        },
    }


def execute_trace(
    config: Mapping[str, object],
    trace_path: str | Path,
    max_workers: int,
    *,
    request_rate: float,
    repeat: int = 0,
    metadata: Mapping[str, object],
    server_instance_id: str,
    server_attempt_id: str,
    tokenizer: TokenizerLike | None = None,
    api_key: str | None = None,
    stream_factory: StreamFactory | None = None,
    max_p99_arrival_lag_ms: float = 100.0,
    clock_ns: Callable[[], int] = time.monotonic_ns,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Replay one trace with at most ``max_workers`` submitted requests."""

    validated = validate_config(config)
    workers = _positive_int(max_workers, "max_workers")
    rate = _positive_float(request_rate, "request_rate")
    repeat_number = _nonnegative_int(repeat, "repeat")
    lag_limit = _positive_float(max_p99_arrival_lag_ms, "max_p99_arrival_lag_ms")
    server_id = _canonical_uuid(server_instance_id, "server_instance_id")
    attempt_id = _canonical_uuid(server_attempt_id, "server_attempt_id")
    if attempt_id == server_id:
        raise ValueError("server_attempt_id must differ from server_instance_id")
    path = Path(trace_path)
    trace = read_trace(path)
    prompt_tokens, output_tokens = _validate_balanced_trace(validated, trace)
    trace_id = trace_fingerprint(trace)
    trace_sha256 = sha256_file(path)
    active_tokenizer = tokenizer
    base_prompt = _comparison_base_prompt(validated)
    prepared = {
        request.request_id: deterministic_prompt_token_ids(
            active_tokenizer, request, base_prompt
        )
        for request in trace
    }

    semaphore = threading.BoundedSemaphore(workers)
    state_lock = threading.Lock()
    in_flight = 0
    max_in_flight = 0
    futures: list[Future[dict[str, object]]] = []
    origin_ns = clock_ns()

    def release_capacity(_future: Future[dict[str, object]]) -> None:
        nonlocal in_flight
        with state_lock:
            in_flight -= 1
        semaphore.release()

    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="week04-trace"
    ) as executor:
        for request in trace:
            deadline_ns = origin_ns + request.scheduled_arrival_ns
            delay_ns = deadline_ns - clock_ns()
            if delay_ns > 0:
                sleep(delay_ns / NANOSECONDS_PER_SECOND)
            capacity_started_ns = clock_ns()
            semaphore.acquire()
            submitted_ns = clock_ns()
            capacity_wait_ns = max(0, submitted_ns - capacity_started_ns)
            with state_lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            try:
                future = executor.submit(
                    run_one,
                    validated,
                    request,
                    prepared[request.request_id],
                    trace_id=trace_id,
                    origin_ns=origin_ns,
                    submitted_ns=submitted_ns,
                    capacity_wait_ns=capacity_wait_ns,
                    api_key=api_key,
                    stream_factory=stream_factory,
                    clock_ns=clock_ns,
                )
            except BaseException:
                with state_lock:
                    in_flight -= 1
                semaphore.release()
                raise
            future.add_done_callback(release_capacity)
            futures.append(future)
        records = [future.result() for future in as_completed(futures)]

    records.sort(key=lambda record: int(record["ordinal"]))
    if [record["request_id"] for record in records] != [
        item.request_id for item in trace
    ]:
        raise RuntimeError("trace replay did not preserve every request row in order")
    summary = summarize_records(records, max_p99_arrival_lag_ms=lag_limit)
    bound_identity = artifact_identity(metadata)
    source_identity = _mapping(metadata.get("source"), "metadata.source")
    model_identity = _mapping(metadata.get("model_identity"), "metadata.model_identity")
    case_id = f"open-balanced-trace-{trace_id[:12]}-r{repeat_number}"
    measurement_counts = summary["measurement_requests"]
    metrics = summary["metrics"]
    tokens = summary["tokens"]
    assert isinstance(measurement_counts, Mapping)
    assert isinstance(metrics, Mapping)
    assert isinstance(tokens, Mapping)
    return {
        "schema_version": SCHEMA_VERSION,
        "adapter": {"name": "week03-trace-replay", "version": "1.0"},
        "captured_at": utc_now(),
        **bound_identity,
        "server_instance_id": server_id,
        "server_attempt_id": attempt_id,
        "identity": {
            "model": model_identity["model"],
            "model_revision": model_identity["model_revision"],
            "dtype": model_identity["dtype"],
        },
        "trace": {
            "trace_path": str(path),
            "trace_id": trace_id,
            "trace_file_sha256": trace_sha256,
        },
        "case": {
            "case_id": case_id,
            "mode": "open-loop",
            "run_id": metadata["run_id"],
            "server_instance_id": server_id,
            "server_attempt_id": attempt_id,
            "git_commit": source_identity["git_commit"],
            "workload": "balanced",
            "requested_prompt_tokens": prompt_tokens,
            "requested_output_tokens": output_tokens,
            "request_rate": rate,
            "concurrency": None,
            "repeat": repeat_number,
            "trace_id": trace_id,
            "trace_file_sha256": trace_sha256,
            "warmup_requests": summary["warmup_requests"]["requested"],
            "measurement_requests": measurement_counts["requested"],
        },
        "counts": dict(measurement_counts),
        "tokens": {
            "requested_prompt_per_request": prompt_tokens,
            "requested_output_per_request": output_tokens,
            **dict(tokens),
        },
        "metrics": dict(metrics),
        "execution": {
            "executor": "ThreadPoolExecutor",
            "server_instance_id": server_id,
            "server_attempt_id": attempt_id,
            "max_workers": workers,
            "max_observed_in_flight": max_in_flight,
            "submission_queue_bound": workers,
            "all_requests": summary["all_requests"],
            "warmup_requests": summary["warmup_requests"],
            "measurement_requests": measurement_counts,
            "arrival_lag_gate": summary["arrival_lag_gate"],
        },
        "equivalence_contract": {
            "same_trace_id": True,
            "same_trace_file_sha256_recorded": True,
            "same_request_ids": True,
            "same_arrival_offsets": True,
            "same_warmup_measurement_flags": True,
            "same_requested_token_shapes": True,
            "identical_prompt_token_ids": True,
            "byte_identical_prompt_claimed": True,
        },
        "records": records,
    }


def _comparison_config(config: Mapping[str, object]) -> Mapping[str, Any]:
    benchmark = _mapping(config.get("benchmark"), "benchmark")
    return _mapping(benchmark.get("comparison"), "benchmark.comparison")


def _default_output_path(
    config: Mapping[str, object],
    trace_path: str | Path,
    request_rate: float,
    repeat: int,
) -> Path:
    output = _mapping(config.get("output"), "output")
    directory = Path(
        str(output.get("trace_replay_dir", "results/week04/raw/trace-replay"))
    )
    rate = f"{request_rate:g}".replace(".", "p")
    return directory / f"{Path(trace_path).stem}-q{rate}-r{repeat}.json"


def _read_week03_events(path: str | Path) -> list[dict[str, str]]:
    source = Path(path)
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError(f"Week 3 event evidence is missing or empty: {source}")
    with source.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != EVENT_FIELDS:
            raise ValueError(
                "Week 3 events header does not match the exact schema: "
                f"expected {EVENT_FIELDS}, got {reader.fieldnames}"
            )
        rows = list(reader)
    if not rows:
        raise ValueError("Week 3 event evidence contains no rows")
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["case_id"], []).append(row)
    for case_rows in grouped.values():
        validate_event_rows(case_rows)
    return rows


def _trace_inventory(directory: str | Path) -> dict[str, tuple[Path, str]]:
    root = Path(directory)
    if not root.is_dir():
        raise ValueError(f"Week 3 arrival trace directory is missing: {root}")
    inventory: dict[str, tuple[Path, str]] = {}
    for path in sorted(root.glob("*.csv")):
        trace = read_trace(path)
        trace_id = trace_fingerprint(trace)
        if trace_id in inventory:
            other = inventory[trace_id][0]
            raise ValueError(
                f"duplicate semantic trace_id in {other} and {path}: {trace_id}"
            )
        inventory[trace_id] = (path, sha256_file(path))
    if not inventory:
        raise ValueError(f"Week 3 arrival trace directory has no trace CSVs: {root}")
    return inventory


def _read_week03_verification(
    comparison: Mapping[str, object],
    week03_config: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    """Require an already completed receipt for freshly verified Week 3 evidence."""

    # This is deliberately the pure verifier, not its CLI wrapper: Week 4 must
    # validate its prerequisite without mutating Week 3's receipt or status.
    from scripts.verify_week03 import verify as verify_week03

    output = _mapping(week03_config.get("output"), "Week 3 output")
    official_paths = {
        "week03_results": Path(str(output["summary_csv"])),
        "week03_run_metadata": Path(str(output["run_metadata"])),
        "week03_events": Path(str(output["raw_events_csv"])),
        "week03_verification_receipt": Path(str(output["verification_receipt"])),
        "week03_run_status": Path(str(output["run_status"])),
        "arrival_trace_dir": Path(str(output["root"])) / "raw" / "traces",
    }
    for name, official_path in official_paths.items():
        if Path(str(comparison[name])).resolve() != official_path.resolve():
            raise ValueError(
                f"Week 4 comparison {name} is not the official Week 3 output path"
            )

    summary = verify_week03(str(comparison["week03_config"]))
    expected_cases = expand_week03_matrix(week03_config, "primary")
    expected_case_ids = {week03_case_id(case) for case in expected_cases}
    expected_trace_keys = {
        (case.profile, case.offered_load_ratio, case.repeat) for case in expected_cases
    }
    if len(expected_cases) != 55 or summary.get("case_count") != len(expected_cases):
        raise ValueError(
            "Week 3 source evidence is not the complete official 55-case matrix"
        )

    receipt_path = Path(str(comparison["week03_verification_receipt"]))
    status_path = Path(str(comparison["week03_run_status"]))
    receipt_value = read_json(receipt_path)
    status_value = read_json(status_path)
    if not isinstance(receipt_value, Mapping):
        raise ValueError("Week 3 verification receipt must be a JSON object")
    if not isinstance(status_value, Mapping):
        raise ValueError("Week 3 run status must be a JSON object")
    receipt = dict(receipt_value)
    status = dict(status_value)
    if receipt.get("schema_version") != 2 or receipt.get("status") != "completed":
        raise ValueError("Week 3 verification receipt is not completed")
    if (
        status.get("status") != "completed"
        or status.get("exit_code") != 0
        or status.get("verification_receipt") != str(receipt_path)
    ):
        raise ValueError("Week 3 run status does not record completed verification")

    stable_fields = (
        "run_id",
        "profile",
        "backend",
        "metadata_fingerprint",
        "calibration_id",
        "case_count",
        "event_count",
        "batch_count",
    )
    differing = [
        field for field in stable_fields if receipt.get(field) != summary.get(field)
    ]
    if differing:
        raise ValueError(
            "Week 3 verification receipt differs from current verified evidence: "
            f"{differing}"
        )
    receipt_hashes = _mapping(
        receipt.get("artifact_sha256"),
        "Week 3 verification receipt artifact_sha256",
    )
    current_hashes = _mapping(
        summary.get("artifact_sha256"),
        "current Week 3 verification artifact_sha256",
    )
    # The Week 3 CLI promotes run-status.json after computing its receipt, so
    # that one mutable lifecycle hash cannot be stable. Every scientific and
    # report artifact must still match; current status is validated separately
    # above and both files are hashed into the Week 4 comparison manifest.
    stable_artifacts = set(current_hashes).difference({"run_status"})
    if set(receipt_hashes).difference({"run_status"}) != stable_artifacts or any(
        receipt_hashes.get(name) != current_hashes.get(name)
        for name in stable_artifacts
    ):
        raise ValueError(
            "Week 3 verification receipt artifact hashes differ from current evidence"
        )
    if status.get("run_id") != summary.get("run_id"):
        raise ValueError("Week 3 run status belongs to a different run")
    if summary.get("profile") != "primary" or summary.get("backend") != "hf":
        raise ValueError("Week 3 source evidence is not primary real-HF evidence")
    return (
        summary,
        receipt,
        {
            "status": status,
            "case_ids": expected_case_ids,
            "cases": expected_cases,
            "trace_keys": expected_trace_keys,
        },
    )


def configured_replay_cases(
    config: Mapping[str, object],
) -> list[dict[str, object]]:
    """Derive replay cases only from complete, verified official Week 3 evidence."""

    validated = validate_config(config)
    comparison = _comparison_config(validated)
    week03_config = load_yaml(str(comparison["week03_config"]))
    verification, _, verification_state = _read_week03_verification(
        comparison, week03_config
    )
    metadata_path = Path(str(comparison["week03_run_metadata"]))
    metadata_value = read_json(metadata_path)
    if not isinstance(metadata_value, Mapping):
        raise ValueError("Week 3 run metadata must be a JSON object")
    week03_metadata = dict(metadata_value)
    validate_week03_run_metadata(
        week03_metadata,
        week03_config,
        profile=str(comparison["profile"]),
        backend=str(comparison["backend"]),
    )
    for field in (
        "run_id",
        "profile",
        "backend",
        "metadata_fingerprint",
        "calibration_id",
    ):
        if week03_metadata.get(field) != verification.get(field):
            raise ValueError(
                f"Week 3 run metadata differs from verified receipt for {field}"
            )
    rows = _read_week03_events(str(comparison["week03_events"]))
    if any(str(row["run_id"]) != str(week03_metadata["run_id"]) for row in rows):
        raise ValueError("Week 3 events contain a run_id different from run metadata")
    if any(
        str(row["metadata_fingerprint"]) != str(week03_metadata["metadata_fingerprint"])
        for row in rows
    ):
        raise ValueError(
            "Week 3 events contain a metadata fingerprint different from run metadata"
        )
    observed_case_ids = {str(row["case_id"]) for row in rows}
    if observed_case_ids != verification_state["case_ids"]:
        raise ValueError(
            "Week 3 events do not contain the exact official 55-case matrix"
        )

    trace_root = Path(str(comparison["arrival_trace_dir"]))
    inventory = _trace_inventory(trace_root)
    expected_trace_paths = {
        trace_root / (f"{profile}-load-{ratio.replace('.', 'p')}-repeat-{repeat}.csv")
        for profile, ratio, repeat in verification_state["trace_keys"]
    }
    actual_trace_paths = {path.resolve() for path, _ in inventory.values()}
    expected_trace_paths = {path.resolve() for path in expected_trace_paths}
    if actual_trace_paths != expected_trace_paths:
        missing = sorted(
            str(path) for path in expected_trace_paths - actual_trace_paths
        )
        extra = sorted(str(path) for path in actual_trace_paths - expected_trace_paths)
        raise ValueError(
            "Week 3 trace directory does not contain the exact primary trace set; "
            f"missing={missing}, extra={extra}"
        )
    expected_trace_ids = {
        key: trace_fingerprint(read_trace(path))
        for key, path in (
            (
                key,
                trace_root
                / (f"{key[0]}-load-{key[1].replace('.', 'p')}-" f"repeat-{key[2]}.csv"),
            )
            for key in verification_state["trace_keys"]
        )
    }
    case_trace_keys = {
        week03_case_id(case): (case.profile, case.offered_load_ratio, case.repeat)
        for case in verification_state["cases"]
    }
    grouped: dict[str, dict[str, object]] = {}
    for line, row in enumerate(rows, start=2):
        if str(row["profile"]) != str(comparison["profile"]):
            continue
        trace_id = str(row["trace_id"])
        trace_key = case_trace_keys[str(row["case_id"])]
        if trace_id != expected_trace_ids[trace_key]:
            raise ValueError(
                f"Week 3 event row {line} does not match its configured trace file"
            )
        candidate = {
            "trace_id": trace_id,
            "request_rate": _positive_float(
                row["arrival_rate_rps"], f"Week 3 event row {line} arrival_rate_rps"
            ),
            "repeat": _nonnegative_int(
                int(row["repeat"]), f"Week 3 event row {line} repeat"
            ),
        }
        existing = grouped.get(trace_id)
        if existing is None:
            grouped[trace_id] = candidate
        elif existing != candidate:
            raise ValueError(
                f"Week 3 trace_id {trace_id} maps to inconsistent rate/repeat values"
            )
    if not grouped:
        raise ValueError(
            "Week 3 events have no rows for the configured primary profile"
        )
    if len(grouped) != len(verification_state["trace_keys"]):
        raise ValueError("Week 3 events do not cover the exact primary trace matrix")

    cases: list[dict[str, object]] = []
    for trace_id, candidate in grouped.items():
        trace_entry = inventory.get(trace_id)
        if trace_entry is None:
            raise ValueError(f"Week 3 trace file is missing for trace_id {trace_id}")
        path, digest = trace_entry
        trace = read_trace(path)
        _validate_balanced_trace(validated, trace)
        trace_request_ids = {request.request_id for request in trace}
        event_rows = [row for row in rows if row["trace_id"] == trace_id]
        event_request_ids = {row["request_id"] for row in event_rows}
        if event_request_ids != trace_request_ids:
            raise ValueError(
                f"Week 3 event/trace request IDs differ for trace_id {trace_id}"
            )
        trace_measurement = {
            request.request_id: request.measurement for request in trace
        }
        for row in event_rows:
            if (
                _parse_bool(row["measurement"], "measurement")
                != trace_measurement[row["request_id"]]
            ):
                raise ValueError(
                    f"Week 3 measurement flag differs from trace for {row['request_id']}"
                )
        cases.append(
            {
                **candidate,
                "trace_path": str(path),
                "trace_file_sha256": digest,
            }
        )

    return sorted(
        cases,
        key=lambda item: (
            float(item["request_rate"]),
            int(item["repeat"]),
            str(item["trace_id"]),
        ),
    )


def _validate_reusable_replay(
    payload: object,
    *,
    metadata: Mapping[str, object],
    case: Mapping[str, object],
    max_workers: int,
    server_instance_id: str,
    server_attempt_id: str,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError("existing trace replay artifact must be a JSON object")
    result = dict(payload)
    validate_artifact_identity(result, metadata, "trace replay artifact")
    if result.get("server_instance_id") != server_instance_id:
        raise ValueError(
            "existing trace replay artifact differs for server_instance_id"
        )
    if result.get("server_attempt_id") != server_attempt_id:
        raise ValueError("existing trace replay artifact differs for server_attempt_id")
    trace_provenance = _mapping(result.get("trace"), "trace replay trace")
    replay_case = _mapping(result.get("case"), "trace replay case")
    execution = _mapping(result.get("execution"), "trace replay execution")
    expected = {
        "trace_id": case["trace_id"],
        "trace_file_sha256": case["trace_file_sha256"],
    }
    for field, value in expected.items():
        if trace_provenance.get(field) != value or replay_case.get(field) != value:
            raise ValueError(f"existing trace replay artifact differs for {field}")
    if float(replay_case.get("request_rate", 0)) != float(case["request_rate"]):
        raise ValueError("existing trace replay artifact differs for request_rate")
    if int(replay_case.get("repeat", -1)) != int(case["repeat"]):
        raise ValueError("existing trace replay artifact differs for repeat")
    if int(execution.get("max_workers", 0)) != max_workers:
        raise ValueError("existing trace replay artifact differs for max_workers")
    records = result.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("existing trace replay artifact has no request records")
    trace = read_trace(str(case["trace_path"]))
    if len(records) != len(trace):
        raise ValueError(
            "existing trace replay artifact does not preserve every trace row"
        )
    trace_id = trace_fingerprint(trace)
    for position, (record, request) in enumerate(zip(records, trace, strict=True)):
        if not isinstance(record, Mapping):
            raise ValueError(
                f"existing trace replay record {position} is not an object"
            )
        expected_trace_fields = _trace_request_payload(request, trace_id)
        for field, value in expected_trace_fields.items():
            if record.get(field) != value:
                raise ValueError(
                    f"existing trace replay record {position} differs for {field}"
                )
        if record.get("status") not in {"completed", "timeout", "error"}:
            raise ValueError(
                f"existing trace replay record {position} has invalid status"
            )
        if record.get("terminal_ns") is None or record.get("arrival_lag_ns") is None:
            raise ValueError(
                f"existing trace replay record {position} lacks terminal/lag evidence"
            )
    gate = _mapping(execution.get("arrival_lag_gate"), "arrival lag gate")
    lag_limit = _positive_float(
        gate.get("max_p99_arrival_lag_ms"), "arrival lag gate limit"
    )
    summary = summarize_records(records, max_p99_arrival_lag_ms=lag_limit)
    if result.get("counts") != summary["measurement_requests"]:
        raise ValueError("existing trace replay aggregate counts are inconsistent")
    if result.get("metrics") != summary["metrics"]:
        raise ValueError("existing trace replay aggregate metrics are inconsistent")
    for field in (
        "all_requests",
        "warmup_requests",
        "measurement_requests",
        "arrival_lag_gate",
    ):
        if execution.get(field) != summary[field]:
            raise ValueError(f"existing trace replay execution differs for {field}")
    observed_in_flight = int(execution.get("max_observed_in_flight", 0))
    if observed_in_flight < 1 or observed_in_flight > max_workers:
        raise ValueError("existing trace replay violates the executor bound")
    return result


def run_all_configured(
    config: Mapping[str, object],
    *,
    metadata: Mapping[str, object],
    server_instance_id: str,
    server_attempt_id: str,
    max_workers: int | None = None,
    api_key: str | None = None,
    tokenizer: TokenizerLike | None = None,
    stream_factory: StreamFactory | None = None,
) -> dict[str, object]:
    """Run/resume every authoritative Week 3 trace and publish one manifest."""

    validated = validate_config(config)
    comparison = _comparison_config(validated)
    output = _mapping(validated["output"], "output")
    workers = _positive_int(max_workers or comparison["max_workers"], "max_workers")
    server_id = _canonical_uuid(server_instance_id, "server_instance_id")
    attempt_id = _canonical_uuid(server_attempt_id, "server_attempt_id")
    if attempt_id == server_id:
        raise ValueError("server_attempt_id must differ from server_instance_id")
    lag_limit = _positive_float(
        comparison["max_p99_arrival_lag_ms"], "max_p99_arrival_lag_ms"
    )
    replay_dir = Path(str(output["trace_replay_dir"]))
    replay_dir.mkdir(parents=True, exist_ok=True)
    cases = configured_replay_cases(validated)
    active_tokenizer = tokenizer or load_tokenizer(validated)
    records: list[dict[str, object]] = []
    artifacts: list[dict[str, object]] = []
    for case in cases:
        destination = _default_output_path(
            validated,
            str(case["trace_path"]),
            float(case["request_rate"]),
            int(case["repeat"]),
        )
        if destination.is_file():
            try:
                result = _validate_reusable_replay(
                    read_json(destination),
                    metadata=metadata,
                    case=case,
                    max_workers=workers,
                    server_instance_id=server_id,
                    server_attempt_id=attempt_id,
                )
            except (KeyError, OSError, TypeError, ValueError) as error:
                raise ValueError(
                    f"refusing to overwrite invalid trace replay artifact {destination}: {error}"
                ) from error
        else:
            if active_tokenizer is None:
                active_tokenizer = load_tokenizer(validated)
            result = execute_trace(
                validated,
                str(case["trace_path"]),
                workers,
                request_rate=float(case["request_rate"]),
                repeat=int(case["repeat"]),
                metadata=metadata,
                server_instance_id=server_id,
                server_attempt_id=attempt_id,
                tokenizer=active_tokenizer,
                api_key=api_key,
                stream_factory=stream_factory,
                max_p99_arrival_lag_ms=lag_limit,
            )
            write_json(destination, result)
        lag_gate = _mapping(
            _mapping(result["execution"], "trace replay execution").get(
                "arrival_lag_gate"
            ),
            "trace replay arrival lag gate",
        )
        if lag_gate.get("passed") is not True:
            raise RuntimeError(
                f"trace replay exceeded the arrival-lag gate: {destination}"
            )
        records.append(
            {
                key: result[key]
                for key in (
                    "schema_version",
                    "adapter",
                    "run_id",
                    "config_fingerprint",
                    "runtime_fingerprint",
                    "model_identity",
                    "model_identity_fingerprint",
                    "server_instance_id",
                    "server_attempt_id",
                    "source",
                    "trace",
                    "identity",
                    "case",
                    "counts",
                    "tokens",
                    "metrics",
                    "execution",
                )
            }
        )
        artifacts.append(
            {
                "case_id": result["case"]["case_id"],
                "trace_id": case["trace_id"],
                "trace_file_sha256": case["trace_file_sha256"],
                "path": str(destination),
                "sha256": sha256_file(destination),
                "arrival_lag_gate": result["execution"]["arrival_lag_gate"],
            }
        )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        **artifact_identity(metadata),
        "server_instance_id": server_id,
        "server_attempt_id": attempt_id,
        "week03_inputs": {
            "config": {
                "path": str(comparison["week03_config"]),
                "sha256": sha256_file(str(comparison["week03_config"])),
            },
            "run_metadata": {
                "path": str(comparison["week03_run_metadata"]),
                "sha256": sha256_file(str(comparison["week03_run_metadata"])),
            },
            "events": {
                "path": str(comparison["week03_events"]),
                "sha256": sha256_file(str(comparison["week03_events"])),
            },
            "summary": {
                "path": str(comparison["week03_results"]),
                "sha256": sha256_file(str(comparison["week03_results"])),
            },
            "verification_receipt": {
                "path": str(comparison["week03_verification_receipt"]),
                "sha256": sha256_file(str(comparison["week03_verification_receipt"])),
            },
            "run_status": {
                "path": str(comparison["week03_run_status"]),
                "sha256": sha256_file(str(comparison["week03_run_status"])),
            },
            "arrival_trace_dir": str(comparison["arrival_trace_dir"]),
        },
        "case_count": len(records),
        "artifacts": artifacts,
        "records": records,
    }
    write_json(str(output["comparison_manifest"]), manifest)
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay one exact Week 3 trace against the Week 4 server."
    )
    parser.add_argument("--config", default="configs/week04.yaml")
    parser.add_argument("--trace", help="Immutable Week 3 trace CSV")
    parser.add_argument("--request-rate", type=float)
    parser.add_argument("--repeat", type=int, default=0)
    parser.add_argument("--max-workers", type=int)
    parser.add_argument("--run-metadata")
    parser.add_argument("--api-key")
    parser.add_argument("--output")
    parser.add_argument("--all-configured", action="store_true")
    parser.add_argument("--plan", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = validate_config(load_yaml(args.config))
    comparison = _comparison_config(config)
    max_workers = args.max_workers or int(comparison.get("max_workers", 16))
    if args.all_configured:
        if (
            args.trace is not None
            or args.request_rate is not None
            or args.output is not None
        ):
            raise ValueError(
                "--all-configured cannot be combined with --trace, --request-rate, or --output"
            )
        if args.plan:
            print(json.dumps(configured_replay_cases(config), indent=2, sort_keys=True))
            return
        metadata = load_run_metadata(config, args.run_metadata)
        server_identity = load_ready_server_identity(config, metadata)
        manifest = run_all_configured(
            config,
            metadata=metadata,
            **server_identity,
            max_workers=max_workers,
            api_key=args.api_key,
        )
        print(
            f"Trace replay manifest completed: {manifest['case_count']} cases -> "
            f"{config['output']['comparison_manifest']}"
        )
        return
    if args.trace is None or args.request_rate is None:
        raise ValueError(
            "--trace and --request-rate are required without --all-configured"
        )
    if args.plan:
        print(
            json.dumps(
                plan_trace(
                    config,
                    args.trace,
                    request_rate=args.request_rate,
                    repeat=args.repeat,
                    max_workers=max_workers,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return
    metadata = load_run_metadata(config, args.run_metadata)
    server_identity = load_ready_server_identity(config, metadata)
    result = execute_trace(
        config,
        args.trace,
        max_workers,
        request_rate=args.request_rate,
        repeat=args.repeat,
        metadata=metadata,
        **server_identity,
        api_key=args.api_key,
        max_p99_arrival_lag_ms=float(comparison.get("max_p99_arrival_lag_ms", 100.0)),
    )
    destination = (
        Path(args.output)
        if args.output
        else _default_output_path(config, args.trace, args.request_rate, args.repeat)
    )
    write_json(destination, result)
    print(f"Trace replay completed; wrote {destination}")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"Trace client failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
