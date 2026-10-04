from __future__ import annotations

import hashlib
import json

import pytest

from src.vllm_result_adapter import (
    ADAPTER_SCHEMA_VERSION,
    load_vllm_result,
    normalize_vllm_result,
)


def raw_result() -> dict[str, object]:
    return {
        "vllm_version": "0.10.2",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "model_revision": "a" * 40,
        "dtype": "bfloat16",
        "workload": "balanced",
        "random_input_len": 256,
        "random_output_len": 64,
        "request_rate": 4.0,
        "max_concurrency": 8,
        "repeat": 1,
        "num_prompts": 3,
        "completed": 2,
        "timeout_count": 1,
        "error_count": 0,
        "errors": [],
        "duration": 2.0,
        "total_input_tokens": 510,
        "total_output_tokens": 126,
        "input_lens": [255, 255],
        "output_lens": [63, 63],
        "request_throughput": 1.0,
        "output_throughput": 63.0,
        "median_ttft_ms": 10.0,
        "p95_ttft_ms": 18.0,
        "p99_ttft_ms": 20.0,
        "median_tpot_ms": 2.0,
        "p95_tpot_ms": 3.0,
        "p99_tpot_ms": 4.0,
        "median_e2el_ms": 130.0,
        "p95_e2el_ms": 150.0,
        "p99_e2el_ms": 160.0,
        "server_queue_ms": 5.0,
    }


def test_normalizes_official_fields_without_losing_requested_or_actual_counts() -> None:
    result = normalize_vllm_result(raw_result())

    assert result["schema_version"] == ADAPTER_SCHEMA_VERSION
    assert result["source"]["vllm_version"] == "0.10.2"
    assert len(result["source"]["sha256"]) == 64
    assert result["case"]["requested_prompt_tokens"] == 256
    assert result["tokens"]["requested_output_per_request"] == 64
    assert result["tokens"]["actual_input_per_request"] == [255, 255]
    assert result["tokens"]["actual_output_total"] == 126
    assert result["counts"] == {
        "requested": 3,
        "success": 2,
        "timeout": 1,
        "error": 0,
    }


def test_safe_aliases_are_accepted_only_when_they_agree() -> None:
    payload = raw_result()
    payload["p50_ttft_ms"] = payload["median_ttft_ms"]
    assert normalize_vllm_result(payload)["metrics"]["p50_ttft_ms"] == 10.0

    payload["p50_ttft_ms"] = 11.0
    with pytest.raises(ValueError, match="Ambiguous.*p50_ttft_ms"):
        normalize_vllm_result(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("duration", float("nan"), "finite and non-negative"),
        ("p99_ttft_ms", -1, "finite and non-negative"),
        ("completed", 1.5, "must be an integer"),
    ],
)
def test_rejects_nonfinite_negative_and_noninteger_fields(
    field: str, value: object, message: str
) -> None:
    payload = raw_result()
    payload[field] = value
    with pytest.raises(ValueError, match=message):
        normalize_vllm_result(payload)


def test_rejects_missing_required_metric_and_unsupported_version() -> None:
    payload = raw_result()
    del payload["p99_ttft_ms"]
    with pytest.raises(ValueError, match="Missing required.*p99_ttft_ms"):
        normalize_vllm_result(payload)

    payload = raw_result()
    payload["vllm_version"] = "0.11.0"
    with pytest.raises(ValueError, match="Unsupported vLLM result version"):
        normalize_vllm_result(payload)


def test_rejects_unclassified_requests_and_inconsistent_actual_totals() -> None:
    payload = raw_result()
    payload["timeout_count"] = 0
    with pytest.raises(ValueError, match="Request counts are incomplete"):
        normalize_vllm_result(payload)

    payload = raw_result()
    payload["total_output_tokens"] = 127
    with pytest.raises(ValueError, match="disagrees"):
        normalize_vllm_result(payload)


def test_file_loader_hashes_exact_source_bytes(tmp_path) -> None:
    path = tmp_path / "vllm.json"
    raw = json.dumps(raw_result(), indent=2).encode() + b"\n"
    path.write_bytes(raw)

    result = load_vllm_result(path)

    assert result["source"]["sha256"] == hashlib.sha256(raw).hexdigest()


def test_normalized_execution_carries_server_attempt_identity() -> None:
    result = normalize_vllm_result(
        raw_result(),
        metadata={
            "server_instance_id": "12345678-1234-4234-8234-123456789abc",
            "server_attempt_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        },
    )

    assert result["execution"]["server_instance_id"] == (
        "12345678-1234-4234-8234-123456789abc"
    )
    assert result["execution"]["server_attempt_id"] == (
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
