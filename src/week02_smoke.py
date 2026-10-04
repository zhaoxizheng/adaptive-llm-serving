from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Mapping, Sequence

from src.common import utc_now

SMOKE_SCHEMA_VERSION = 1
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


def token_sequence_hash(tokens: Sequence[int]) -> str:
    if any(isinstance(token, bool) or not isinstance(token, int) for token in tokens):
        raise TypeError("generated token sequences must contain only integers")
    serialized = json.dumps(list(tokens), separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def generated_tokens_sha256(rows: Sequence[Sequence[int]]) -> str:
    normalized: list[list[int]] = []
    for row in rows:
        if any(isinstance(token, bool) or not isinstance(token, int) for token in row):
            raise TypeError("generated token rows must contain only integers")
        normalized.append(list(row))
    serialized = json.dumps(normalized, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def smoke_workload(config: Mapping[str, object]) -> tuple[list[int], int, int]:
    benchmark = config.get("benchmark")
    if not isinstance(benchmark, Mapping):
        raise ValueError("benchmark must be a mapping")
    raw_sizes = benchmark.get("smoke_batch_sizes")
    if not isinstance(raw_sizes, list) or not raw_sizes:
        raise ValueError("benchmark.smoke_batch_sizes must be a non-empty list")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in raw_sizes):
        raise ValueError("benchmark.smoke_batch_sizes must contain integers")
    sizes = [int(value) for value in raw_sizes]
    if any(value < 1 for value in sizes) or len(set(sizes)) != len(sizes):
        raise ValueError("benchmark.smoke_batch_sizes must be unique positive integers")
    if 1 not in sizes:
        raise ValueError("benchmark.smoke_batch_sizes must include batch size 1 for parity")

    sweeps = benchmark.get("sweeps")
    batch_sweep = sweeps.get("batch_size") if isinstance(sweeps, Mapping) else None
    if not isinstance(batch_sweep, Mapping):
        raise ValueError("benchmark.sweeps.batch_size must be a mapping")
    prompt_tokens = batch_sweep.get("prompt_tokens")
    output_tokens = batch_sweep.get("output_tokens")
    if (
        isinstance(prompt_tokens, bool)
        or not isinstance(prompt_tokens, int)
        or prompt_tokens < 1
        or isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or output_tokens < 1
    ):
        raise ValueError("The smoke workload requires fixed positive prompt/output lengths")
    return sizes, prompt_tokens, output_tokens


def create_smoke_artifact(
    config: Mapping[str, object],
    metadata: Mapping[str, object],
    cases: Sequence[Mapping[str, object]],
    *,
    input_token_ids: Sequence[int],
    batched_request_tokens: Sequence[int],
    reference_request_tokens: Sequence[int],
) -> dict[str, object]:
    sizes, prompt_tokens, output_tokens = smoke_workload(config)
    batched_hash = token_sequence_hash(batched_request_tokens)
    reference_hash = token_sequence_hash(reference_request_tokens)
    if list(batched_request_tokens) != list(reference_request_tokens):
        raise ValueError("Batch-size-1 output differs from the single-request reference")
    source = metadata.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("metadata.source must be a mapping")
    return {
        "schema_version": SMOKE_SCHEMA_VERSION,
        "status": "completed",
        "created_at": utc_now(),
        "run_id": metadata["run_id"],
        "git_commit": source["git_commit"],
        "config_fingerprint": metadata["config_fingerprint"],
        "runtime_fingerprint": metadata["runtime_fingerprint"],
        "smoke_batch_sizes": sizes,
        "workload": {
            "prompt_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "use_cache": True,
            "decoding": "greedy",
        },
        "cases": [dict(case) for case in cases],
        "batch_1_parity": {
            "status": "matched",
            "reference": "src.inference.run_greedy_generation",
            "input_token_ids": list(input_token_ids),
            "input_token_hash": token_sequence_hash(input_token_ids),
            "batched_token_ids": list(batched_request_tokens),
            "reference_token_ids": list(reference_request_tokens),
            "batched_request_hash": batched_hash,
            "reference_request_hash": reference_hash,
            "output_tokens": output_tokens,
        },
    }


def run_preformal_smoke(
    tokenizer: object,
    model: object,
    config: Mapping[str, object],
    metadata: Mapping[str, object],
    *,
    baseline: object,
    dtype: str,
) -> dict[str, object]:
    """Run configured shape smoke cells and exact Week 1 batch=1 parity.

    GPU-related modules are imported only inside this function so importing and
    validating smoke artifacts remains CPU-only. Any exception aborts before the
    formal matrix; callers may atomically retain ``failed_smoke_artifact``.
    """

    from src.hf_batch_backend import prepare_batch_tensors, run_batched_greedy_generation
    from src.inference import build_exact_length_input, run_greedy_generation
    from src.kv_cache_estimator import (
        estimate_from_model_config,
        observed_kv_sequence_length,
    )

    sizes, prompt_tokens, output_tokens = smoke_workload(config)
    generation = config.get("generation")
    if not isinstance(generation, Mapping):
        raise ValueError("generation must be a mapping")
    prompt = str(generation["prompt"])
    padding_side = str(generation.get("padding_side", "left"))
    model_config = getattr(model, "config", None)
    if model_config is None:
        raise ValueError("Loaded model has no config")
    theoretical = estimate_from_model_config(
        model_config,
        sequence_length=observed_kv_sequence_length(prompt_tokens, output_tokens),
        batch_size=1,
        dtype=dtype,
    )

    cases: list[dict[str, object]] = []
    batched_request_tokens: list[int] | None = None
    batch_one_input = None
    for batch_size in sizes:
        host_input, host_mask, preprocessing_ms = prepare_batch_tensors(
            tokenizer,
            prompt,
            batch_size=batch_size,
            prompt_tokens=prompt_tokens,
            padding_side=padding_side,
        )
        estimate_bytes = theoretical.bytes * batch_size
        result, generated = run_batched_greedy_generation(
            model,
            host_input,
            host_mask,
            output_tokens=output_tokens,
            preprocessing_ms=preprocessing_ms,
            baseline=baseline,
            theoretical_kv_cache_bytes=estimate_bytes,
        )
        cases.append(
            {
                "status": "completed",
                "batch_size": batch_size,
                "input_shape": list(map(int, host_input.shape)),
                "attention_mask_shape": list(map(int, host_mask.shape)),
                "output_shape": [len(generated), len(generated[0])],
                "actual_output_tokens": result.actual_output_tokens,
                "generated_token_ids": generated,
                "request_output_hashes": [
                    token_sequence_hash(tokens) for tokens in generated
                ],
                "output_token_hash": result.output_token_hash,
            }
        )
        if batch_size == 1:
            batch_one_input = host_input
            batched_request_tokens = generated[0]

    if batch_one_input is None or batched_request_tokens is None:
        raise RuntimeError("Configured smoke omitted the required batch-size-1 case")
    reference_input, _ = build_exact_length_input(tokenizer, prompt, prompt_tokens)
    if (
        tuple(reference_input.shape) != tuple(batch_one_input.shape)
        or not bool(reference_input.equal(batch_one_input))
    ):
        raise ValueError(
            "Week 2 batch-size-1 input IDs differ from the Week 1 input builder"
        )
    _, reference_request_tokens = run_greedy_generation(
        model,
        reference_input,
        output_tokens,
        use_cache=True,
        tokenization_ms=0.0,
    )
    payload = create_smoke_artifact(
        config,
        metadata,
        cases,
        input_token_ids=reference_input.tolist()[0],
        batched_request_tokens=batched_request_tokens,
        reference_request_tokens=reference_request_tokens,
    )
    validate_smoke_artifact(payload, config, metadata)
    return payload


def failed_smoke_artifact(
    metadata: Mapping[str, object],
    *,
    phase: str,
    error: BaseException,
) -> dict[str, object]:
    source = metadata.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("metadata.source must be a mapping")
    return {
        "schema_version": SMOKE_SCHEMA_VERSION,
        "status": "failed",
        "created_at": utc_now(),
        "run_id": metadata["run_id"],
        "git_commit": source["git_commit"],
        "config_fingerprint": metadata["config_fingerprint"],
        "runtime_fingerprint": metadata["runtime_fingerprint"],
        "error_phase": phase,
        "error_type": error.__class__.__name__,
        "error_message": " ".join(str(error).split())[:2_000],
    }


def _shape(value: object, expected: list[int], name: str) -> None:
    if value != expected:
        raise ValueError(f"Smoke {name} must be {expected}, got {value!r}")


def validate_smoke_artifact(
    payload: Mapping[str, object],
    config: Mapping[str, object],
    metadata: Mapping[str, object],
) -> None:
    sizes, prompt_tokens, output_tokens = smoke_workload(config)
    if payload.get("schema_version") != SMOKE_SCHEMA_VERSION:
        raise ValueError("Unsupported or missing Week 2 smoke schema_version")
    if payload.get("status") != "completed":
        raise ValueError("Week 2 pre-formal smoke did not complete successfully")
    try:
        datetime.fromisoformat(str(payload["created_at"]))
    except (KeyError, ValueError) as error:
        raise ValueError("Week 2 smoke has an invalid created_at") from error
    source = metadata.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("metadata.source must be a mapping")
    identity = {
        "run_id": metadata.get("run_id"),
        "git_commit": source.get("git_commit"),
        "config_fingerprint": metadata.get("config_fingerprint"),
        "runtime_fingerprint": metadata.get("runtime_fingerprint"),
    }
    for field, expected in identity.items():
        if payload.get(field) != expected:
            raise ValueError(f"Smoke identity mismatch for {field}")
    if payload.get("smoke_batch_sizes") != sizes:
        raise ValueError("Smoke artifact does not cover configured smoke_batch_sizes")
    expected_workload = {
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "use_cache": True,
        "decoding": "greedy",
    }
    if payload.get("workload") != expected_workload:
        raise ValueError("Smoke workload differs from the configured batch sweep")

    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or len(raw_cases) != len(sizes):
        raise ValueError("Smoke artifact has an incomplete case list")
    cases: dict[int, Mapping[str, object]] = {}
    for case in raw_cases:
        if not isinstance(case, Mapping):
            raise ValueError("Smoke case must be a mapping")
        batch_size = case.get("batch_size")
        if not isinstance(batch_size, int) or batch_size in cases:
            raise ValueError("Smoke case has an invalid or duplicate batch_size")
        cases[batch_size] = case
    if set(cases) != set(sizes):
        raise ValueError("Smoke cases do not match configured smoke_batch_sizes")
    for batch_size in sizes:
        case = cases[batch_size]
        if case.get("status") != "completed":
            raise ValueError(f"Smoke batch size {batch_size} did not complete")
        _shape(case.get("input_shape"), [batch_size, prompt_tokens], "input_shape")
        _shape(
            case.get("attention_mask_shape"),
            [batch_size, prompt_tokens],
            "attention_mask_shape",
        )
        _shape(case.get("output_shape"), [batch_size, output_tokens], "output_shape")
        if case.get("actual_output_tokens") != batch_size * output_tokens:
            raise ValueError("Smoke case has inconsistent actual_output_tokens")
        generated = case.get("generated_token_ids")
        if (
            not isinstance(generated, list)
            or len(generated) != batch_size
            or any(
                not isinstance(row, list)
                or len(row) != output_tokens
                or any(isinstance(token, bool) or not isinstance(token, int) for token in row)
                for row in generated
            )
        ):
            raise ValueError("Smoke case has invalid generated_token_ids")
        expected_aggregate_hash = generated_tokens_sha256(generated)
        if case.get("output_token_hash") != expected_aggregate_hash:
            raise ValueError("Smoke case has an invalid output_token_hash")
        request_hashes = case.get("request_output_hashes")
        if (
            not isinstance(request_hashes, list)
            or len(request_hashes) != batch_size
            or any(not HASH_PATTERN.fullmatch(str(value)) for value in request_hashes)
        ):
            raise ValueError("Smoke case has invalid per-request output hashes")
        if request_hashes != [token_sequence_hash(row) for row in generated]:
            raise ValueError("Smoke per-request output hashes do not match token IDs")

    parity = payload.get("batch_1_parity")
    if not isinstance(parity, Mapping) or parity.get("status") != "matched":
        raise ValueError("Smoke artifact lacks successful batch-size-1 parity")
    if parity.get("reference") != "src.inference.run_greedy_generation":
        raise ValueError("Smoke parity names an unexpected reference implementation")
    batched_tokens = parity.get("batched_token_ids")
    reference_tokens = parity.get("reference_token_ids")
    input_tokens = parity.get("input_token_ids")
    if (
        not isinstance(input_tokens, list)
        or len(input_tokens) != prompt_tokens
        or any(isinstance(token, bool) or not isinstance(token, int) for token in input_tokens)
        or parity.get("input_token_hash") != token_sequence_hash(input_tokens)
    ):
        raise ValueError("Smoke parity has invalid input token evidence")
    if (
        not isinstance(batched_tokens, list)
        or not isinstance(reference_tokens, list)
        or batched_tokens != reference_tokens
        or len(batched_tokens) != output_tokens
        or any(isinstance(token, bool) or not isinstance(token, int) for token in batched_tokens)
    ):
        raise ValueError("Smoke batch-size-1 parity token IDs differ")
    batched_hash = str(parity.get("batched_request_hash", ""))
    reference_hash = str(parity.get("reference_request_hash", ""))
    if (
        not HASH_PATTERN.fullmatch(batched_hash)
        or batched_hash != reference_hash
        or batched_hash != token_sequence_hash(batched_tokens)
        or cases[1].get("request_output_hashes") != [batched_hash]
        or cases[1].get("generated_token_ids") != [batched_tokens]
        or parity.get("output_tokens") != output_tokens
    ):
        raise ValueError("Smoke batch-size-1 parity hashes are inconsistent")


def validate_smoke_formal_hashes(
    payload: Mapping[str, object],
    rows: Sequence[Mapping[str, str]],
) -> None:
    workload = payload.get("workload")
    raw_cases = payload.get("cases")
    if not isinstance(workload, Mapping) or not isinstance(raw_cases, list):
        raise ValueError("Smoke artifact is missing workload or cases")
    prompt_tokens = int(workload["prompt_tokens"])
    output_tokens = int(workload["output_tokens"])
    smoke_hashes = {
        int(case["batch_size"]): str(case["output_token_hash"])
        for case in raw_cases
        if isinstance(case, Mapping)
    }
    for row in rows:
        if (
            row.get("status") == "completed"
            and int(row["prompt_tokens"]) == prompt_tokens
            and int(row["output_tokens"]) == output_tokens
            and int(row["batch_size"]) in smoke_hashes
            and row["output_token_hash"] != smoke_hashes[int(row["batch_size"])]
        ):
            raise ValueError(
                "Formal output hash differs from pre-formal smoke for workload "
                f"({row['batch_size']}, {prompt_tokens}, {output_tokens})"
            )
