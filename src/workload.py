from __future__ import annotations

import csv
import math
import os
import random
import tempfile
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Mapping, Sequence

TRACE_SCHEMA_VERSION = 1
TRACE_FIELDS = [
    "schema_version",
    "trace_id",
    "ordinal",
    "request_id",
    "scheduled_arrival_ns",
    "prompt_tokens",
    "output_tokens",
    "workload_class",
    "measurement",
]


@dataclass(frozen=True, slots=True)
class TraceRequest:
    """One portable request in a pre-generated arrival trace.

    ``scheduled_arrival_ns`` is an offset from the start of a case. It is not a
    value sampled from a process-local monotonic clock, so the same trace can be
    replayed by Week 3 and Week 4.
    """

    ordinal: int
    request_id: str
    scheduled_arrival_ns: int
    prompt_tokens: int
    output_tokens: int
    workload_class: str = "fixed"
    measurement: bool = True

    @property
    def scheduled_offset_ns(self) -> int:
        return self.scheduled_arrival_ns


def _validate_generation_inputs(
    *, arrival_rate_rps: float, duration_ns: int, prompt_tokens: int, output_tokens: int
) -> None:
    if not math.isfinite(arrival_rate_rps) or arrival_rate_rps <= 0:
        raise ValueError("arrival_rate_rps must be finite and positive")
    if duration_ns <= 0:
        raise ValueError("duration_ns must be positive")
    if prompt_tokens <= 0 or output_tokens <= 0:
        raise ValueError("prompt_tokens and output_tokens must be positive")


def generate_poisson_trace(
    *,
    arrival_rate_rps: float,
    seed: int,
    prompt_tokens: int,
    output_tokens: int,
    duration_ns: int | None = None,
    duration_seconds: float | None = None,
    warmup_ns: int = 0,
    max_requests: int | None = None,
    workload_classes: Sequence[Mapping[str, object]] | None = None,
) -> tuple[TraceRequest, ...]:
    """Generate all Poisson arrivals before a serving run starts.

    Arrival and shape selection use separate seeded random streams. Consequently,
    changing a mixed-length probability does not silently change arrival times.
    The first request follows an exponential inter-arrival from case time zero.
    """

    if (duration_ns is None) == (duration_seconds is None):
        raise ValueError("provide exactly one of duration_ns or duration_seconds")
    if duration_seconds is not None:
        if not math.isfinite(duration_seconds) or duration_seconds <= 0:
            raise ValueError("duration_seconds must be finite and positive")
        duration_ns = round(duration_seconds * 1_000_000_000)
    assert duration_ns is not None
    _validate_generation_inputs(
        arrival_rate_rps=arrival_rate_rps,
        duration_ns=duration_ns,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
    )
    if warmup_ns < 0 or warmup_ns >= duration_ns:
        raise ValueError("warmup_ns must be nonnegative and less than duration_ns")
    if max_requests is not None and max_requests <= 0:
        raise ValueError("max_requests must be positive when provided")

    classes: tuple[Mapping[str, object], ...] = tuple(workload_classes or ())
    cumulative: list[tuple[float, Mapping[str, object]]] = []
    if classes:
        total_weight = 0.0
        for item in classes:
            weight = float(item.get("weight", 0))
            item_prompt = int(item.get("prompt_tokens", 0))
            item_output = int(item.get("output_tokens", 0))
            name = str(item.get("name", "")).strip()
            if not name or not math.isfinite(weight) or weight <= 0:
                raise ValueError("each workload class needs a name and positive weight")
            if item_prompt <= 0 or item_output <= 0:
                raise ValueError("workload class token counts must be positive")
            total_weight += weight
            cumulative.append((total_weight, item))
        cumulative = [(limit / total_weight, item) for limit, item in cumulative]

    arrival_rng = random.Random(seed)
    shape_rng = random.Random(seed ^ 0x9E3779B97F4A7C15)
    current_seconds = 0.0
    requests: list[TraceRequest] = []
    while True:
        current_seconds += arrival_rng.expovariate(arrival_rate_rps)
        arrival_ns = round(current_seconds * 1_000_000_000)
        if arrival_ns >= duration_ns:
            break
        ordinal = len(requests)
        selected_prompt = prompt_tokens
        selected_output = output_tokens
        selected_class = "fixed"
        if cumulative:
            draw = shape_rng.random()
            selected = cumulative[-1][1]
            for limit, candidate in cumulative:
                if draw < limit:
                    selected = candidate
                    break
            selected_prompt = int(selected["prompt_tokens"])
            selected_output = int(selected["output_tokens"])
            selected_class = str(selected["name"])
        requests.append(
            TraceRequest(
                ordinal=ordinal,
                request_id=f"req-{ordinal:06d}",
                scheduled_arrival_ns=arrival_ns,
                prompt_tokens=selected_prompt,
                output_tokens=selected_output,
                workload_class=selected_class,
                measurement=arrival_ns >= warmup_ns,
            )
        )
        if max_requests is not None and len(requests) >= max_requests:
            break

    validate_trace(requests, duration_ns=duration_ns)
    return tuple(requests)


def validate_trace(
    requests: Sequence[TraceRequest], *, duration_ns: int | None = None
) -> None:
    seen_ids: set[str] = set()
    seen_ordinals: set[int] = set()
    previous_key: tuple[int, int] | None = None
    for position, request in enumerate(requests):
        if request.ordinal < 0 or request.ordinal in seen_ordinals:
            raise ValueError(f"duplicate or negative trace ordinal: {request.ordinal}")
        if not request.request_id or request.request_id in seen_ids:
            raise ValueError(f"duplicate or blank request_id: {request.request_id!r}")
        if request.scheduled_arrival_ns < 0:
            raise ValueError("scheduled_arrival_ns must be nonnegative")
        if duration_ns is not None and request.scheduled_arrival_ns >= duration_ns:
            raise ValueError("trace request falls outside the configured duration")
        if request.prompt_tokens <= 0 or request.output_tokens <= 0:
            raise ValueError("trace token counts must be positive")
        if not request.workload_class:
            raise ValueError("workload_class must not be blank")
        key = (request.scheduled_arrival_ns, request.ordinal)
        if previous_key is not None and key < previous_key:
            raise ValueError(f"trace is not ordered at position {position}")
        previous_key = key
        seen_ids.add(request.request_id)
        seen_ordinals.add(request.ordinal)


def _canonical_payload(requests: Sequence[TraceRequest]) -> bytes:
    lines = []
    for request in requests:
        values = asdict(request)
        values["measurement"] = bool(values["measurement"])
        lines.append(
            "|".join(
                str(values[field])
                for field in (
                    "ordinal",
                    "request_id",
                    "scheduled_arrival_ns",
                    "prompt_tokens",
                    "output_tokens",
                    "workload_class",
                    "measurement",
                )
            )
        )
    return ("\n".join(lines) + "\n").encode()


def trace_fingerprint(requests: Sequence[TraceRequest]) -> str:
    validate_trace(requests)
    return sha256(_canonical_payload(requests)).hexdigest()


def _parse_bool(value: object) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def write_trace(path: str | Path, requests: Sequence[TraceRequest]) -> str:
    """Atomically persist a trace and return its content fingerprint."""

    validate_trace(requests)
    trace_id = trace_fingerprint(requests)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=TRACE_FIELDS)
            writer.writeheader()
            for request in requests:
                writer.writerow(
                    {
                        "schema_version": TRACE_SCHEMA_VERSION,
                        "trace_id": trace_id,
                        "ordinal": request.ordinal,
                        "request_id": request.request_id,
                        "scheduled_arrival_ns": request.scheduled_arrival_ns,
                        "prompt_tokens": request.prompt_tokens,
                        "output_tokens": request.output_tokens,
                        "workload_class": request.workload_class,
                        "measurement": str(request.measurement).lower(),
                    }
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return trace_id


def read_trace(path: str | Path) -> tuple[TraceRequest, ...]:
    source = Path(path)
    with source.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != TRACE_FIELDS:
            raise ValueError(
                f"trace header does not match schema: expected {TRACE_FIELDS}, "
                f"got {reader.fieldnames}"
            )
        rows = list(reader)
    requests: list[TraceRequest] = []
    trace_ids: set[str] = set()
    for line, row in enumerate(rows, start=2):
        try:
            if int(row["schema_version"]) != TRACE_SCHEMA_VERSION:
                raise ValueError("unsupported schema version")
            trace_ids.add(row["trace_id"])
            requests.append(
                TraceRequest(
                    ordinal=int(row["ordinal"]),
                    request_id=row["request_id"],
                    scheduled_arrival_ns=int(row["scheduled_arrival_ns"]),
                    prompt_tokens=int(row["prompt_tokens"]),
                    output_tokens=int(row["output_tokens"]),
                    workload_class=row["workload_class"],
                    measurement=_parse_bool(row["measurement"]),
                )
            )
        except (KeyError, ValueError) as error:
            raise ValueError(f"invalid trace row {line}: {error}") from error
    validate_trace(requests)
    if len(trace_ids) != 1:
        raise ValueError("trace rows do not contain exactly one trace_id")
    expected = trace_fingerprint(requests)
    if trace_ids != {expected}:
        raise ValueError("trace_id does not match trace contents")
    return tuple(requests)
