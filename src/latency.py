from __future__ import annotations

import math


def summarize_latency(
    *,
    tokenization_ms: float,
    h2d_ms: float,
    prefill_forward_ms: float,
    first_token_selection_ms: float,
    decode_step_ms: list[float],
    output_tokens: int,
) -> dict[str, float | None]:
    if output_tokens < 1:
        raise ValueError("output_tokens must be at least 1")
    if len(decode_step_ms) != output_tokens - 1:
        raise ValueError(
            "decode_step_ms must contain exactly output_tokens - 1 measurements"
        )
    for name, value in {
        "tokenization_ms": tokenization_ms,
        "h2d_ms": h2d_ms,
        "prefill_forward_ms": prefill_forward_ms,
        "first_token_selection_ms": first_token_selection_ms,
    }.items():
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and non-negative")
    if any(not math.isfinite(value) or value < 0 for value in decode_step_ms):
        raise ValueError("decode step measurements must be finite and non-negative")

    inference_ttft_ms = h2d_ms + prefill_forward_ms + first_token_selection_ms
    decode_ms = sum(decode_step_ms)
    total_generation_ms = inference_ttft_ms + decode_ms
    if total_generation_ms <= 0:
        raise ValueError("total generation time must be positive")
    ordered_steps = sorted(decode_step_ms)
    return {
        "inference_ttft_ms": inference_ttft_ms,
        "end_to_end_ttft_ms": tokenization_ms + inference_ttft_ms,
        "decode_ms": decode_ms,
        "mean_tpot_ms": (
            sum(decode_step_ms) / len(decode_step_ms) if decode_step_ms else None
        ),
        "p50_tpot_ms": (
            ordered_steps[max(math.ceil(0.50 * len(ordered_steps)) - 1, 0)]
            if ordered_steps
            else None
        ),
        "p95_tpot_ms": (
            ordered_steps[max(math.ceil(0.95 * len(ordered_steps)) - 1, 0)]
            if ordered_steps
            else None
        ),
        "total_generation_ms": total_generation_ms,
        "end_to_end_ms": tokenization_ms + total_generation_ms,
        "output_tokens_per_second": output_tokens / (total_generation_ms / 1_000),
    }
