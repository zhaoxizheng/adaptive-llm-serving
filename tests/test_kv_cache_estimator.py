from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.kv_cache_estimator import (
    bytes_per_element,
    estimate_from_model_config,
    estimate_kv_cache_bytes,
    model_kv_parameters,
    observed_kv_sequence_length,
)


def test_estimate_formula_uses_k_and_v_and_gqa_heads() -> None:
    assert estimate_kv_cache_bytes(24, 2, 64, 320, 1, 2) == (
        2 * 24 * 2 * 64 * 320 * 1 * 2
    )


def test_observed_peak_sequence_excludes_unprocessed_last_output_token() -> None:
    assert observed_kv_sequence_length(256, 64) == 319
    assert observed_kv_sequence_length(32, 1) == 32


def test_estimate_scales_with_batch_sequence_and_element_width() -> None:
    baseline = estimate_kv_cache_bytes(4, 2, 8, 100, 1, 2)

    assert estimate_kv_cache_bytes(4, 2, 8, 100, 8, 2) == baseline * 8
    assert estimate_kv_cache_bytes(4, 2, 8, 200, 1, 2) == baseline * 2
    assert estimate_kv_cache_bytes(4, 2, 8, 100, 1, 4) == baseline * 2


def test_model_parameters_derive_head_dim_but_keep_kv_heads() -> None:
    config = SimpleNamespace(
        num_hidden_layers=24,
        num_key_value_heads=2,
        hidden_size=896,
        num_attention_heads=14,
    )

    parameters = model_kv_parameters(config)

    assert parameters.num_hidden_layers == 24
    assert parameters.num_key_value_heads == 2
    assert parameters.head_dim == 64


def test_explicit_head_dim_takes_precedence() -> None:
    parameters = model_kv_parameters(
        {
            "num_hidden_layers": 10,
            "num_key_value_heads": 3,
            "head_dim": 80,
            "hidden_size": 999,
            "num_attention_heads": 7,
        }
    )

    assert parameters.head_dim == 80


def test_model_parameters_reject_nondivisible_hidden_size() -> None:
    with pytest.raises(ValueError, match="divisible"):
        model_kv_parameters(
            {
                "num_hidden_layers": 2,
                "num_key_value_heads": 1,
                "hidden_size": 10,
                "num_attention_heads": 3,
            }
        )


def test_estimate_from_model_config_reports_mib() -> None:
    estimate = estimate_from_model_config(
        {
            "num_hidden_layers": 2,
            "num_key_value_heads": 1,
            "hidden_size": 32,
            "num_attention_heads": 4,
        },
        sequence_length=320,
        batch_size=8,
        dtype="bfloat16",
    )

    assert estimate.bytes == 2 * 2 * 1 * 8 * 320 * 8 * 2
    assert estimate.mib == estimate.bytes / (1024**2)
    assert estimate.to_dict()["mib"] == estimate.mib


@pytest.mark.parametrize(
    ("dtype", "expected"),
    [("torch.bfloat16", 2), ("float16", 2), ("float32", 4), ("int8", 1)],
)
def test_dtype_byte_width(dtype: str, expected: int) -> None:
    assert bytes_per_element(dtype) == expected
