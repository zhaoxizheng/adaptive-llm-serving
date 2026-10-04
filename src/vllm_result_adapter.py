"""Normalize ``vllm bench serve`` 0.10.2 JSON into a stable local schema.

This module intentionally has no dependency on vLLM.  Raw benchmark output is
treated as evidence: callers can retain it byte-for-byte and use ``source_sha256``
to bind the normalized record to that evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import argparse
import os
import re
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ADAPTER_SCHEMA_VERSION = 1
ADAPTER_VERSION = "1.0"
SUPPORTED_VLLM_VERSIONS = frozenset({"0.10.2"})


def _equivalent(left: object, right: object) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return left == right


def _pick(
    sources: Sequence[Mapping[str, Any]],
    aliases: Sequence[str],
    name: str,
    *,
    required: bool = True,
    default: object = None,
) -> Any:
    found: list[tuple[str, object]] = []
    for source in sources:
        for alias in aliases:
            if alias in source and source[alias] is not None:
                found.append((alias, source[alias]))
    if not found:
        if required:
            raise ValueError(f"Missing required vLLM result field {name}")
        return default
    first = found[0][1]
    if any(not _equivalent(first, value) for _, value in found[1:]):
        details = ", ".join(f"{alias}={value!r}" for alias, value in found)
        raise ValueError(f"Ambiguous vLLM result field {name}: {details}")
    return first


def _number(value: object, name: str, *, integer: bool = False) -> int | float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric, not boolean")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be numeric") from error
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    if integer:
        if not result.is_integer():
            raise ValueError(f"{name} must be an integer")
        return int(result)
    return result


def _optional_number(
    sources: Sequence[Mapping[str, Any]], aliases: Sequence[str], name: str
) -> float | None:
    value = _pick(sources, aliases, name, required=False)
    return None if value is None else float(_number(value, name))


def _percentile(
    sources: Sequence[Mapping[str, Any]],
    metric: str,
    percentile: int,
    aliases: Sequence[str],
    *,
    required: bool = True,
) -> float | None:
    direct = _pick(sources, aliases, f"p{percentile}_{metric}_ms", required=False)
    collection = _pick(
        sources,
        (f"percentiles_{metric}_ms",),
        f"percentiles_{metric}_ms",
        required=False,
    )
    nested: object = None
    if collection is not None:
        if isinstance(collection, Mapping):
            for key in (str(percentile), f"p{percentile}", percentile):
                if key in collection:
                    nested = collection[key]
                    break
        elif isinstance(collection, list):
            for entry in collection:
                if (
                    isinstance(entry, (list, tuple))
                    and len(entry) == 2
                    and float(entry[0]) == float(percentile)
                ):
                    nested = entry[1]
                    break
    if direct is not None and nested is not None and not _equivalent(direct, nested):
        raise ValueError(f"Ambiguous vLLM result field p{percentile}_{metric}_ms")
    value = direct if direct is not None else nested
    if value is None:
        if required:
            raise ValueError(
                f"Missing required vLLM result field p{percentile}_{metric}_ms"
            )
        return None
    return float(_number(value, f"p{percentile}_{metric}_ms"))


def _string(
    sources: Sequence[Mapping[str, Any]],
    aliases: Sequence[str],
    name: str,
    *,
    required: bool = True,
) -> str | None:
    value = _pick(sources, aliases, name, required=required)
    if value is None:
        return None
    result = str(value).strip()
    if not result:
        raise ValueError(f"{name} must not be empty")
    return result


def _lengths(value: object, name: str) -> list[int]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return [
        int(_number(item, f"{name}[{index}]", integer=True))
        for index, item in enumerate(value)
    ]


def _source_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def normalize_vllm_result(
    payload: Mapping[str, Any],
    *,
    source_sha256: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    vllm_version: str | None = None,
) -> dict[str, Any]:
    """Return one canonical record from a vLLM 0.10.2 bench result.

    Besides flat official output, a collector may wrap it in ``result`` or
    ``benchmark`` and add ``identity``/``case`` metadata.  Aliases are accepted
    only when all supplied aliases agree.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("vLLM result must be a JSON object")
    body_value = payload.get("result", payload.get("benchmark", payload))
    if not isinstance(body_value, Mapping):
        raise ValueError("vLLM result body must be a JSON object")
    body = body_value
    identity = payload.get("identity", {})
    case = payload.get("case", {})
    if not isinstance(identity, Mapping) or not isinstance(case, Mapping):
        raise ValueError("identity and case metadata must be objects")
    supplied_metadata = metadata or {}
    if not isinstance(supplied_metadata, Mapping):
        raise ValueError("metadata must be an object")
    candidates = (body, case, identity, supplied_metadata, payload)
    sources = tuple(
        source
        for index, source in enumerate(candidates)
        if all(source is not previous for previous in candidates[:index])
    )

    if vllm_version is not None:
        supplied_metadata = {**supplied_metadata, "vllm_version": vllm_version}
        candidates = (body, case, identity, supplied_metadata, payload)
        sources = tuple(
            source
            for index, source in enumerate(candidates)
            if all(source is not previous for previous in candidates[:index])
        )
    version = _string(sources, ("vllm_version", "version"), "vllm_version")
    assert version is not None
    version = version.removeprefix("v")
    if version not in SUPPORTED_VLLM_VERSIONS:
        raise ValueError(
            f"Unsupported vLLM result version {version!r}; expected one of "
            f"{sorted(SUPPORTED_VLLM_VERSIONS)}"
        )

    requested = int(
        _number(
            _pick(
                sources,
                ("num_prompts", "request_count", "requested_requests"),
                "requested_requests",
            ),
            "requested_requests",
            integer=True,
        )
    )
    completed = int(
        _number(
            _pick(
                sources,
                ("completed", "completed_requests", "success_count"),
                "success_count",
            ),
            "success_count",
            integer=True,
        )
    )
    timeout_count_value = _pick(
        sources,
        ("timeout_count", "timeouts"),
        "timeout_count",
        required=False,
        default=0,
    )
    timeout_count = int(_number(timeout_count_value, "timeout_count", integer=True))
    errors_value = _pick(
        sources, ("errors", "error_messages"), "errors", required=False, default=[]
    )
    if not isinstance(errors_value, list):
        raise ValueError("errors must be a list")
    explicit_errors = _pick(
        sources, ("error_count", "failed_requests"), "error_count", required=False
    )
    if explicit_errors is not None:
        error_count = int(_number(explicit_errors, "error_count", integer=True))
    elif errors_value:
        actual_errors = [error for error in errors_value if str(error).strip()]
        timeout_errors = sum("timeout" in str(error).lower() for error in actual_errors)
        if timeout_count == 0:
            timeout_count = timeout_errors
        error_count = len(actual_errors) - timeout_errors
    else:
        error_count = requested - completed - timeout_count
    if completed + timeout_count + error_count > requested:
        raise ValueError("success, timeout, and error counts exceed requested requests")
    unclassified = requested - completed - timeout_count - error_count
    if unclassified:
        raise ValueError(
            "Request counts are incomplete; supply timeout_count and error_count "
            f"for the {unclassified} unclassified requests"
        )

    input_lens_value = _pick(
        sources,
        ("input_lens", "actual_input_tokens"),
        "actual_input_tokens",
        required=False,
    )
    output_lens_value = _pick(
        sources,
        ("output_lens", "actual_output_tokens"),
        "actual_output_tokens",
        required=False,
    )
    input_lens = (
        _lengths(input_lens_value, "actual_input_tokens")
        if input_lens_value is not None
        else []
    )
    output_lens = (
        _lengths(output_lens_value, "actual_output_tokens")
        if output_lens_value is not None
        else []
    )
    if input_lens and len(input_lens) not in {completed, requested}:
        raise ValueError(
            "actual_input_tokens length must match success_count or requested requests"
        )
    if output_lens and len(output_lens) not in {completed, requested}:
        raise ValueError(
            "actual_output_tokens length must match success_count or requested requests"
        )
    total_input = int(
        _number(
            _pick(
                sources,
                ("total_input_tokens", "input_token_count"),
                "total_input_tokens",
            ),
            "total_input_tokens",
            integer=True,
        )
    )
    total_output = int(
        _number(
            _pick(
                sources,
                ("total_output_tokens", "output_token_count"),
                "total_output_tokens",
            ),
            "total_output_tokens",
            integer=True,
        )
    )
    if input_lens:
        if len(input_lens) == completed and sum(input_lens) != total_input:
            raise ValueError("total_input_tokens disagrees with actual_input_tokens")
        if (
            len(input_lens) == requested
            and completed == requested
            and sum(input_lens) != total_input
        ):
            raise ValueError("total_input_tokens disagrees with actual_input_tokens")
    if output_lens and sum(output_lens) != total_output:
        raise ValueError("total_output_tokens disagrees with actual_output_tokens")

    requested_prompt = int(
        _number(
            _pick(
                sources,
                ("requested_prompt_tokens", "random_input_len", "prompt_tokens"),
                "requested_prompt_tokens",
            ),
            "requested_prompt_tokens",
            integer=True,
        )
    )
    requested_output = int(
        _number(
            _pick(
                sources,
                ("requested_output_tokens", "random_output_len", "output_tokens"),
                "requested_output_tokens",
            ),
            "requested_output_tokens",
            integer=True,
        )
    )
    request_rate_value = _pick(
        sources,
        ("request_rate", "offered_request_rate"),
        "request_rate",
        required=False,
    )
    concurrency_value = _pick(
        sources, ("max_concurrency", "concurrency"), "concurrency", required=False
    )
    if isinstance(request_rate_value, str) and request_rate_value.lower() in {
        "inf",
        "infinity",
    }:
        request_rate_value = None
    if request_rate_value is None and concurrency_value is None:
        raise ValueError("A request_rate or concurrency case dimension is required")

    metrics = {
        "duration_seconds": float(
            _number(
                _pick(sources, ("duration", "duration_seconds"), "duration_seconds"),
                "duration_seconds",
            )
        ),
        "request_throughput": float(
            _number(
                _pick(
                    sources,
                    ("request_throughput", "requests_per_second"),
                    "request_throughput",
                ),
                "request_throughput",
            )
        ),
        "output_token_throughput": float(
            _number(
                _pick(
                    sources,
                    ("output_throughput", "output_tokens_per_second"),
                    "output_token_throughput",
                ),
                "output_token_throughput",
            )
        ),
        "p50_ttft_ms": float(
            _number(
                _pick(sources, ("median_ttft_ms", "p50_ttft_ms"), "p50_ttft_ms"),
                "p50_ttft_ms",
            )
        ),
        "p95_ttft_ms": _percentile(
            sources, "ttft", 95, ("p95_ttft_ms",), required=False
        ),
        "p99_ttft_ms": _percentile(sources, "ttft", 99, ("p99_ttft_ms",)),
        "p50_tpot_ms": float(
            _number(
                _pick(sources, ("median_tpot_ms", "p50_tpot_ms"), "p50_tpot_ms"),
                "p50_tpot_ms",
            )
        ),
        "p95_tpot_ms": _percentile(
            sources, "tpot", 95, ("p95_tpot_ms",), required=False
        ),
        "p99_tpot_ms": _percentile(sources, "tpot", 99, ("p99_tpot_ms",)),
        "p50_e2e_ms": _optional_number(
            sources, ("median_e2el_ms", "median_e2e_ms", "p50_e2e_ms"), "p50_e2e_ms"
        ),
        "p95_e2e_ms": _percentile(
            sources, "e2el", 95, ("p95_e2el_ms", "p95_e2e_ms"), required=False
        ),
        "p99_e2e_ms": _percentile(sources, "e2el", 99, ("p99_e2el_ms", "p99_e2e_ms")),
        "server_queue_ms": _optional_number(
            sources, ("server_queue_ms", "queue_time_ms"), "server_queue_ms"
        ),
    }
    if metrics["duration_seconds"] <= 0:
        raise ValueError("duration_seconds must be positive")

    model = _string(sources, ("model_id", "model", "model_name"), "model")
    revision = _string(sources, ("model_revision", "revision"), "model_revision")
    dtype = _string(sources, ("dtype",), "dtype")
    workload = _string(sources, ("workload", "workload_name", "label"), "workload")
    case_id = _string(sources, ("case_id",), "case_id", required=False)
    mode = _string(sources, ("benchmark_mode", "mode"), "mode", required=False)
    run_id = _string(sources, ("run_id",), "run_id", required=False)
    git_commit = _string(sources, ("git_commit",), "git_commit", required=False)
    source_tree_fingerprint = _string(
        sources,
        ("source_tree_fingerprint",),
        "source_tree_fingerprint",
        required=False,
    )
    config_identity = _string(
        sources, ("config_fingerprint",), "config_fingerprint", required=False
    )
    runtime_identity = _string(
        sources, ("runtime_fingerprint",), "runtime_fingerprint", required=False
    )
    model_identity_fingerprint = _string(
        sources,
        ("model_identity_fingerprint",),
        "model_identity_fingerprint",
        required=False,
    )
    server_instance_id = _string(
        sources, ("server_instance_id",), "server_instance_id", required=False
    )
    server_attempt_id = _string(
        sources, ("server_attempt_id",), "server_attempt_id", required=False
    )

    digest = source_sha256 or _source_hash(payload)
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("source_sha256 must be a lowercase SHA-256 digest")

    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "adapter": {"name": "vllm-bench-serve", "version": ADAPTER_VERSION},
        "source": {
            "format": "vllm-bench-serve",
            "vllm_version": version,
            "sha256": digest,
        },
        "identity": {"model": model, "model_revision": revision, "dtype": dtype},
        "execution": {
            "run_id": run_id,
            "git_commit": git_commit,
            "source_tree_fingerprint": source_tree_fingerprint,
            "config_fingerprint": config_identity,
            "runtime_fingerprint": runtime_identity,
            "model_identity_fingerprint": model_identity_fingerprint,
            "server_instance_id": server_instance_id,
            "server_attempt_id": server_attempt_id,
        },
        "case": {
            "case_id": case_id,
            "mode": mode,
            "run_id": run_id,
            "git_commit": git_commit,
            "workload": workload,
            "requested_prompt_tokens": requested_prompt,
            "requested_output_tokens": requested_output,
            "request_rate": (
                None
                if request_rate_value is None
                else float(_number(request_rate_value, "request_rate"))
            ),
            "concurrency": (
                None
                if concurrency_value is None
                else int(_number(concurrency_value, "concurrency", integer=True))
            ),
            "repeat": int(
                _number(
                    _pick(sources, ("repeat",), "repeat", required=False, default=0),
                    "repeat",
                    integer=True,
                )
            ),
        },
        "counts": {
            "requested": requested,
            "success": completed,
            "timeout": timeout_count,
            "error": error_count,
        },
        "tokens": {
            "requested_prompt_per_request": requested_prompt,
            "requested_output_per_request": requested_output,
            "actual_input_per_request": input_lens,
            "actual_output_per_request": output_lens,
            "actual_input_total": total_input,
            "actual_output_total": total_output,
        },
        "metrics": metrics,
        "errors": list(errors_value),
    }


def load_vllm_result(path: str | Path) -> dict[str, Any]:
    """Load and normalize one raw JSON artifact, hashing its exact bytes."""

    source = Path(path)
    raw = source.read_bytes()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in {source}: {error}") from error
    metadata: dict[str, Any] = {}
    argv_path = source.with_suffix(".argv.json")
    if argv_path.is_file():
        argv_payload = json.loads(argv_path.read_text(encoding="utf-8"))
        if isinstance(argv_payload, Mapping) and isinstance(
            argv_payload.get("metadata"), Mapping
        ):
            metadata.update(argv_payload["metadata"])
    raw_dir = source.parent.parent
    before_path = raw_dir / "metrics" / f"{source.stem}.before.prom"
    after_path = raw_dir / "metrics" / f"{source.stem}.prom"
    if before_path.is_file() and after_path.is_file():
        before_text = before_path.read_text(encoding="utf-8")
        after_text = after_path.read_text(encoding="utf-8")
        for metric_name in (
            "vllm:request_queue_time_seconds",
            "vllm_request_queue_time_seconds",
        ):
            try:
                metadata["server_queue_ms"] = prometheus_histogram_delta_mean_ms(
                    before_text, after_text, metric_name
                )
                break
            except ValueError:
                continue
        else:
            raise ValueError(
                "Prometheus snapshots lack the vLLM 0.10.2 queue histogram contract"
            )
    normalized = normalize_vllm_result(
        payload, metadata=metadata, source_sha256=hashlib.sha256(raw).hexdigest()
    )
    sidecars: dict[str, dict[str, object]] = {}
    candidates = {
        "argv": argv_path,
        "stdout": source.with_suffix(".stdout.txt"),
        "help": source.with_suffix(".help.txt"),
        "watchdog": source.with_suffix(".watchdog.json"),
        "complete": source.with_suffix(".complete.json"),
        "metrics_before": before_path,
        "metrics_after": after_path,
    }
    for name, sidecar in candidates.items():
        if sidecar.is_file():
            sidecars[name] = {
                "path": str(sidecar),
                "size_bytes": sidecar.stat().st_size,
                "sha256": hashlib.sha256(sidecar.read_bytes()).hexdigest(),
            }
    normalized["source"]["path"] = str(source)
    normalized["source"]["sidecars"] = sidecars
    return normalized


def _prometheus_sum(text: str, metric: str) -> float:
    total = 0.0
    found = False
    pattern = re.compile(rf"^{re.escape(metric)}(?:\{{[^}}]*\}})?\s+([^\s]+)")
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            total += float(_number(match.group(1), metric))
            found = True
    if not found:
        raise ValueError(f"Prometheus snapshot lacks {metric}")
    return total


def prometheus_histogram_delta_mean_ms(before: str, after: str, metric: str) -> float:
    """Return a run-window histogram mean without calling it a percentile."""

    delta_sum = _prometheus_sum(after, metric + "_sum") - _prometheus_sum(
        before, metric + "_sum"
    )
    delta_count = _prometheus_sum(after, metric + "_count") - _prometheus_sum(
        before, metric + "_count"
    )
    if delta_count <= 0 or delta_sum < 0:
        raise ValueError(f"Invalid Prometheus histogram delta for {metric}")
    return delta_sum / delta_count * 1_000


# Short, discoverable aliases for callers that use adapter terminology.
adapt_vllm_result = normalize_vllm_result
load_and_adapt_vllm_result = load_vllm_result


def is_normalized_vllm_result(payload: object) -> bool:
    return (
        isinstance(payload, Mapping)
        and payload.get("schema_version") == ADAPTER_SCHEMA_VERSION
        and isinstance(payload.get("adapter"), Mapping)
        and payload["adapter"].get("name") == "vllm-bench-serve"
    )


def write_normalized_jsonl(
    inputs: Sequence[str | Path], output: str | Path
) -> list[dict[str, Any]]:
    """Normalize raw artifacts and atomically replace one JSONL dataset."""

    records = [load_vllm_result(path) for path in inputs]
    case_ids = [record["case"].get("case_id") for record in records]
    if any(not case_id for case_id in case_ids):
        raise ValueError(
            "Every normalized benchmark result must contain case_id metadata"
        )
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("Raw benchmark inputs contain duplicate case IDs")
    required_execution = (
        "run_id",
        "git_commit",
        "source_tree_fingerprint",
        "config_fingerprint",
        "runtime_fingerprint",
        "model_identity_fingerprint",
        "server_instance_id",
        "server_attempt_id",
    )
    for record in records:
        execution = record.get("execution")
        if not isinstance(execution, Mapping):
            raise ValueError("Every normalized result must contain execution identity")
        missing = [name for name in required_execution if not execution.get(name)]
        if missing:
            raise ValueError(
                f"Normalized case {record['case'].get('case_id')} lacks execution fields: {missing}"
            )
    identities = {
        field: {str(record["execution"].get(field)) for record in records}
        for field in required_execution
    }
    mixed = {field: values for field, values in identities.items() if len(values) != 1}
    if mixed:
        raise ValueError(f"Raw benchmark inputs mix execution identities: {mixed}")
    try:
        uuid.UUID(next(iter(identities["run_id"])))
        uuid.UUID(next(iter(identities["server_instance_id"])))
        uuid.UUID(next(iter(identities["server_attempt_id"])))
    except ValueError as error:
        raise ValueError("Normalized execution IDs must be UUIDs") from error
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return records


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize vLLM 0.10.2 benchmark JSON."
    )
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def _main() -> None:
    args = _parse_args()
    records = write_normalized_jsonl(args.input, args.output)
    print(f"Normalized {len(records)} benchmark artifacts to {args.output}")


if __name__ == "__main__":
    _main()
