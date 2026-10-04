from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


class TokenizerLike(Protocol):
    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]: ...


@dataclass(frozen=True)
class PaddedBatch:
    """A framework-independent padded batch made only from Python lists."""

    input_ids: list[list[int]]
    attention_mask: list[list[int]]
    prompt_lengths: list[int]
    padded_length: int
    padding_side: str

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.input_ids), self.padded_length

    def __iter__(self):
        """Allow ``input_ids, attention_mask = build_static_batch(...)``."""

        yield self.input_ids
        yield self.attention_mask

    def __len__(self) -> int:
        return 2

    def __getitem__(self, index: int) -> list[list[int]]:
        return (self.input_ids, self.attention_mask)[index]


def _validate_token_sequences(token_sequences: Sequence[Sequence[int]]) -> None:
    if not token_sequences:
        raise ValueError("token_sequences must contain at least one request")
    for index, sequence in enumerate(token_sequences):
        if not sequence:
            raise ValueError(f"token sequence {index} is empty")
        if any(isinstance(token, bool) or not isinstance(token, int) for token in sequence):
            raise TypeError(f"token sequence {index} contains a non-integer token")


def build_padded_batch(
    token_sequences: Sequence[Sequence[int]],
    pad_token_id: int,
    *,
    padding_side: str = "left",
    padded_length: int | None = None,
) -> PaddedBatch:
    """Pad token sequences and construct masks without importing tensor libraries.

    ``1`` marks a real token and ``0`` marks padding. ``padded_length`` can be
    larger than the longest request but may never truncate an input implicitly.
    """

    _validate_token_sequences(token_sequences)
    if isinstance(pad_token_id, bool) or not isinstance(pad_token_id, int):
        raise TypeError("pad_token_id must be an integer")
    if padding_side not in {"left", "right"}:
        raise ValueError("padding_side must be 'left' or 'right'")

    prompt_lengths = [len(sequence) for sequence in token_sequences]
    minimum_length = max(prompt_lengths)
    target_length = minimum_length if padded_length is None else padded_length
    if isinstance(target_length, bool) or not isinstance(target_length, int):
        raise TypeError("padded_length must be an integer")
    if target_length < minimum_length:
        raise ValueError("padded_length cannot truncate the longest input")

    input_rows: list[list[int]] = []
    mask_rows: list[list[int]] = []
    for sequence in token_sequences:
        tokens = list(sequence)
        padding = target_length - len(tokens)
        if padding_side == "left":
            input_rows.append([pad_token_id] * padding + tokens)
            mask_rows.append([0] * padding + [1] * len(tokens))
        else:
            input_rows.append(tokens + [pad_token_id] * padding)
            mask_rows.append([1] * len(tokens) + [0] * padding)

    return PaddedBatch(
        input_ids=input_rows,
        attention_mask=mask_rows,
        prompt_lengths=prompt_lengths,
        padded_length=target_length,
        padding_side=padding_side,
    )


def build_static_batch(
    token_sequences: Sequence[Sequence[int]],
    pad_token_id: int,
    *,
    padding_side: str = "left",
    padded_length: int | None = None,
) -> PaddedBatch:
    """Public Week 2 name for the pure-Python padding and mask builder."""

    return build_padded_batch(
        token_sequences,
        pad_token_id,
        padding_side=padding_side,
        padded_length=padded_length,
    )


def exact_length_token_ids(
    tokenizer: TokenizerLike,
    text: str,
    target_tokens: int,
) -> list[int]:
    """Repeat then truncate one encoded prompt to an exact positive length."""

    if isinstance(target_tokens, bool) or not isinstance(target_tokens, int):
        raise TypeError("target_tokens must be an integer")
    if target_tokens < 1:
        raise ValueError("target_tokens must be at least 1")
    encoded = list(tokenizer.encode(text, add_special_tokens=True))
    if not encoded:
        raise ValueError("prompt tokenization produced no tokens")
    repeats = (target_tokens + len(encoded) - 1) // len(encoded)
    return (encoded * repeats)[:target_tokens]


def build_exact_length_batch(
    tokenizer: TokenizerLike,
    prompt: str,
    *,
    batch_size: int,
    target_tokens: int,
    pad_token_id: int,
    padding_side: str = "left",
) -> PaddedBatch:
    """Build a deterministic batch of distinct synthetic request prompts.

    Row zero keeps the configured prompt unchanged for batch-size-one parity. A
    request number is prefixed only for additional rows, so a larger batch does not
    silently duplicate one sequence. Every row is normalized to the target length.
    """

    if isinstance(batch_size, bool) or not isinstance(batch_size, int):
        raise TypeError("batch_size must be an integer")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    sequences = [exact_length_token_ids(tokenizer, prompt, target_tokens)]
    sequences.extend(
        exact_length_token_ids(
            tokenizer,
            f"Synthetic request {request_index:02d}. {prompt}",
            target_tokens,
        )
        for request_index in range(1, batch_size)
    )
    return build_static_batch(
        sequences,
        pad_token_id,
        padding_side=padding_side,
        padded_length=target_tokens,
    )


def _finite_nonnegative(name: str, value: float) -> float:
    import math

    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return float(value)


def _nearest_rank(values: Sequence[float], fraction: float) -> float | None:
    import math

    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(fraction * len(ordered)) - 1, 0)]


def summarize_batch_latency(
    *,
    preprocessing_ms: float,
    h2d_ms: float,
    gpu_ttft_ms: float,
    decode_step_ms: Sequence[float],
    batch_size: int,
    output_tokens: int,
) -> dict[str, float | int | None]:
    """Apply the frozen Week 2 timing and aggregate-throughput contract.

    The first output token is produced by the prefill forward and is therefore not
    an ITL/TPOT sample. Exactly ``output_tokens - 1`` decode intervals are required.
    """

    if batch_size < 1 or output_tokens < 1:
        raise ValueError("batch_size and output_tokens must be at least 1")
    if len(decode_step_ms) != output_tokens - 1:
        raise ValueError(
            "decode_step_ms must contain exactly output_tokens - 1 measurements"
        )
    preprocessing = _finite_nonnegative("preprocessing_ms", preprocessing_ms)
    h2d = _finite_nonnegative("h2d_ms", h2d_ms)
    ttft = _finite_nonnegative("gpu_ttft_ms", gpu_ttft_ms)
    decode = [
        _finite_nonnegative(f"decode_step_ms[{index}]", value)
        for index, value in enumerate(decode_step_ms)
    ]
    generation_ms = ttft + sum(decode)
    if generation_ms <= 0:
        raise ValueError("generation_ms must be positive")
    actual_output_tokens = batch_size * output_tokens
    return {
        "preprocessing_ms": preprocessing,
        "h2d_ms": h2d,
        "gpu_ttft_ms": ttft,
        "e2e_ttft_ms": preprocessing + h2d + ttft,
        "mean_tpot_ms": sum(decode) / len(decode) if decode else None,
        "p95_itl_ms": _nearest_rank(decode, 0.95),
        "generation_ms": generation_ms,
        "e2e_latency_ms": preprocessing + h2d + generation_ms,
        "actual_output_tokens": actual_output_tokens,
        "output_tokens_per_second": actual_output_tokens / (generation_ms / 1_000),
        "requests_per_second": batch_size / (generation_ms / 1_000),
    }
