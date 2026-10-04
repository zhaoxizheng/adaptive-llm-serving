from __future__ import annotations

import sys

import pytest

from src.batch_inference import (
    build_exact_length_batch,
    build_static_batch,
    summarize_batch_latency,
)
from src.hf_batch_backend import deterministic_request_token_ids


class FakeTokenizer:
    pad_token_id = 0

    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        assert add_special_tokens is True
        return [sum(map(ord, word)) % 251 + 1 for word in text.split()] or [1]


def test_build_static_batch_left_padding_is_cpu_pure() -> None:
    torch_before = sys.modules.get("torch")
    batch = build_static_batch([[11, 12], [21]], 0, padding_side="left")

    assert batch.shape == (2, 2)
    assert batch.input_ids == [[11, 12], [0, 21]]
    assert batch.attention_mask == [[1, 1], [0, 1]]
    assert batch.prompt_lengths == [2, 1]
    assert sys.modules.get("torch") is torch_before


def test_build_static_batch_right_padding_and_explicit_width() -> None:
    batch = build_static_batch(
        [[1, 2], [3]],
        99,
        padding_side="right",
        padded_length=4,
    )

    input_ids, attention_mask = batch
    assert input_ids == [[1, 2, 99, 99], [3, 99, 99, 99]]
    assert attention_mask == [[1, 1, 0, 0], [1, 0, 0, 0]]
    assert [sum(row) for row in attention_mask] == [2, 1]


def test_build_static_batch_never_truncates_implicitly() -> None:
    with pytest.raises(ValueError, match="cannot truncate"):
        build_static_batch([[1, 2, 3]], 0, padded_length=2)


def test_exact_length_batch_has_controlled_shape_and_distinct_requests() -> None:
    batch = build_exact_length_batch(
        FakeTokenizer(),
        "hello model",
        batch_size=3,
        target_tokens=7,
        pad_token_id=0,
    )

    assert batch.shape == (3, 7)
    assert all(len(row) == 7 for row in batch.input_ids)
    assert all(sum(mask) == 7 for mask in batch.attention_mask)
    assert len({tuple(row) for row in batch.input_ids}) == 3
    expected_first = FakeTokenizer().encode("hello model", add_special_tokens=True)
    expected_first = (expected_first * 7)[:7]
    assert batch.input_ids[0] == expected_first


def test_row_zero_is_stable_between_batch_one_and_larger_batch() -> None:
    tokenizer = FakeTokenizer()
    single = build_exact_length_batch(
        tokenizer,
        "same prompt",
        batch_size=1,
        target_tokens=8,
        pad_token_id=0,
    )
    larger = build_exact_length_batch(
        tokenizer,
        "same prompt",
        batch_size=4,
        target_tokens=8,
        pad_token_id=0,
    )

    assert larger.input_ids[0] == single.input_ids[0]
    assert len({tuple(row) for row in larger.input_ids}) == 4


def test_trace_request_tokens_do_not_depend_on_batch_membership_or_position() -> None:
    tokenizer = FakeTokenizer()
    expected = deterministic_request_token_ids(
        tokenizer, "req-000042", "same prompt", 8
    )
    before = deterministic_request_token_ids(
        tokenizer, "req-000001", "same prompt", 8
    )
    after = deterministic_request_token_ids(
        tokenizer, "req-000099", "same prompt", 8
    )

    assert deterministic_request_token_ids(
        tokenizer, "req-000042", "same prompt", 8
    ) == expected
    assert expected not in (before, after)


def test_summarize_batch_latency_uses_aggregate_output_tokens() -> None:
    result = summarize_batch_latency(
        preprocessing_ms=2.0,
        h2d_ms=1.0,
        gpu_ttft_ms=5.0,
        decode_step_ms=[3.0, 7.0, 10.0],
        batch_size=4,
        output_tokens=4,
    )

    assert result["e2e_ttft_ms"] == 8.0
    assert result["generation_ms"] == 25.0
    assert result["e2e_latency_ms"] == 28.0
    assert result["mean_tpot_ms"] == pytest.approx(20 / 3)
    assert result["p95_itl_ms"] == 10.0
    assert result["actual_output_tokens"] == 16
    assert result["output_tokens_per_second"] == 640.0
    assert result["requests_per_second"] == 160.0


def test_summarize_batch_latency_requires_one_less_decode_interval() -> None:
    with pytest.raises(ValueError, match="output_tokens - 1"):
        summarize_batch_latency(
            preprocessing_ms=1.0,
            h2d_ms=1.0,
            gpu_ttft_ms=1.0,
            decode_step_ms=[1.0],
            batch_size=1,
            output_tokens=3,
        )
