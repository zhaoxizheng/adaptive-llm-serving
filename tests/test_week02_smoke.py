from __future__ import annotations

from copy import deepcopy

import pytest

from src.week02_smoke import (
    create_smoke_artifact,
    generated_tokens_sha256,
    token_sequence_hash,
    validate_smoke_artifact,
    validate_smoke_formal_hashes,
)
from src.week02_contract import create_run_metadata
from tests.test_week02_contract import canonical_config, make_runtime, make_source


def smoke_cases() -> list[dict[str, object]]:
    cases = []
    for batch_size in (1, 2, 4):
        generated = [[request, 10, 11] + list(range(61)) for request in range(batch_size)]
        cases.append(
            {
                "status": "completed",
                "batch_size": batch_size,
                "input_shape": [batch_size, 256],
                "attention_mask_shape": [batch_size, 256],
                "output_shape": [batch_size, 64],
                "actual_output_tokens": batch_size * 64,
                "generated_token_ids": generated,
                "request_output_hashes": [token_sequence_hash(row) for row in generated],
                "output_token_hash": generated_tokens_sha256(generated),
            }
        )
    return cases


def artifact() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    config = canonical_config()
    metadata = create_run_metadata(config, make_source(), make_runtime())
    first_tokens = smoke_cases()[0]["generated_token_ids"][0]
    payload = create_smoke_artifact(
        config,
        metadata,
        smoke_cases(),
        input_token_ids=list(range(256)),
        batched_request_tokens=first_tokens,
        reference_request_tokens=first_tokens,
    )
    return config, metadata, payload


def test_smoke_artifact_covers_shapes_hashes_and_parity() -> None:
    config, metadata, payload = artifact()

    validate_smoke_artifact(payload, config, metadata)


def test_smoke_rejects_missing_size_and_tampered_token_ids() -> None:
    config, metadata, payload = artifact()
    payload["cases"] = payload["cases"][:-1]
    with pytest.raises(ValueError, match="incomplete case list"):
        validate_smoke_artifact(payload, config, metadata)

    config, metadata, payload = artifact()
    payload["cases"][0]["generated_token_ids"][0][0] += 1
    with pytest.raises(ValueError, match="output_token_hash"):
        validate_smoke_artifact(payload, config, metadata)


def test_smoke_rejects_identity_or_parity_mismatch() -> None:
    config, metadata, payload = artifact()
    changed = deepcopy(payload)
    changed["runtime_fingerprint"] = "wrong"
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_smoke_artifact(changed, config, metadata)

    changed = deepcopy(payload)
    changed["batch_1_parity"]["reference_request_hash"] = "f" * 64
    with pytest.raises(ValueError, match="parity hashes"):
        validate_smoke_artifact(changed, config, metadata)


def test_smoke_rejects_different_parity_token_ids() -> None:
    config, metadata, payload = artifact()
    payload["batch_1_parity"]["reference_token_ids"][-1] += 1

    with pytest.raises(ValueError, match="parity token IDs differ"):
        validate_smoke_artifact(payload, config, metadata)


def test_formal_overlapping_workload_must_match_smoke_hash() -> None:
    _, _, payload = artifact()
    matching_hash = payload["cases"][0]["output_token_hash"]
    row = {
        "status": "completed",
        "batch_size": "1",
        "prompt_tokens": "256",
        "output_tokens": "64",
        "output_token_hash": matching_hash,
    }
    validate_smoke_formal_hashes(payload, [row])

    row["output_token_hash"] = "f" * 64
    with pytest.raises(ValueError, match="differs from pre-formal smoke"):
        validate_smoke_formal_hashes(payload, [row])
