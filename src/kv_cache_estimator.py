from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, NamedTuple


DTYPE_BYTES = {
    "bfloat16": 2,
    "bf16": 2,
    "float16": 2,
    "fp16": 2,
    "half": 2,
    "float32": 4,
    "fp32": 4,
    "float": 4,
    "float64": 8,
    "fp64": 8,
    "int8": 1,
    "uint8": 1,
}


@dataclass(frozen=True)
class KVCacheEstimate:
    num_hidden_layers: int
    num_key_value_heads: int
    head_dim: int
    sequence_length: int
    batch_size: int
    bytes_per_element: int
    bytes: int

    @property
    def mib(self) -> float:
        return self.bytes / (1024**2)

    def to_dict(self) -> dict[str, int | float]:
        return {**asdict(self), "mib": self.mib}


class KVModelParameters(NamedTuple):
    num_hidden_layers: int
    num_key_value_heads: int
    head_dim: int


def bytes_per_element(dtype: str) -> int:
    normalized = str(dtype).lower().removeprefix("torch.")
    try:
        return DTYPE_BYTES[normalized]
    except KeyError as error:
        raise ValueError(f"Unsupported KV cache dtype: {dtype!r}") from error


def _positive_integer(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def estimate_kv_cache_bytes(
    num_layers: int,
    num_kv_heads: int,
    head_dim: int,
    sequence_length: int,
    batch_size: int = 1,
    bytes_per_element: int = 2,
) -> int:
    """Return theoretical K/V tensor bytes for a decoder-only model."""

    values = {
        "num_layers": num_layers,
        "num_kv_heads": num_kv_heads,
        "head_dim": head_dim,
        "sequence_length": sequence_length,
        "batch_size": batch_size,
        "bytes_per_element": bytes_per_element,
    }
    checked = {name: _positive_integer(name, value) for name, value in values.items()}
    return (
        2
        * checked["num_layers"]
        * checked["num_kv_heads"]
        * checked["head_dim"]
        * checked["sequence_length"]
        * checked["batch_size"]
        * checked["bytes_per_element"]
    )


def observed_kv_sequence_length(prompt_tokens: int, output_tokens: int) -> int:
    """Return the largest cache length observed while producing fixed outputs.

    The first output token comes from the prompt prefill. Producing ``N`` output
    tokens therefore performs only ``N - 1`` cache-extending decode forwards.
    """

    prompt = _positive_integer("prompt_tokens", prompt_tokens)
    output = _positive_integer("output_tokens", output_tokens)
    return prompt + output - 1


def _config_value(config: object, name: str) -> object:
    if isinstance(config, Mapping):
        if name not in config:
            raise ValueError(f"Model config is missing {name}")
        return config[name]
    if not hasattr(config, name):
        raise ValueError(f"Model config is missing {name}")
    return getattr(config, name)


def model_kv_parameters(config: object) -> KVModelParameters:
    """Read layers, GQA/MQA KV heads, and head dimension from model config."""

    layers = int(_config_value(config, "num_hidden_layers"))
    kv_heads = int(_config_value(config, "num_key_value_heads"))
    explicit_head_dim = (
        config.get("head_dim")
        if isinstance(config, Mapping)
        else getattr(config, "head_dim", None)
    )
    if explicit_head_dim is None:
        hidden_size = int(_config_value(config, "hidden_size"))
        attention_heads = int(_config_value(config, "num_attention_heads"))
        _positive_integer("num_attention_heads", attention_heads)
        if hidden_size % attention_heads:
            raise ValueError("hidden_size must be divisible by num_attention_heads")
        head_dim = hidden_size // attention_heads
    else:
        head_dim = int(explicit_head_dim)
    return KVModelParameters(
        _positive_integer("num_hidden_layers", layers),
        _positive_integer("num_key_value_heads", kv_heads),
        _positive_integer("head_dim", head_dim),
    )


def estimate_from_model_config(
    config: object,
    *,
    sequence_length: int,
    batch_size: int = 1,
    dtype: str | None = None,
    element_bytes: int | None = None,
) -> KVCacheEstimate:
    """Estimate KV cache size from a Transformers config or plain mapping."""

    layers, kv_heads, head_dim = model_kv_parameters(config)

    if element_bytes is None:
        configured_dtype = dtype
        if configured_dtype is None:
            configured_dtype = (
                config.get("torch_dtype")
                if isinstance(config, Mapping)
                else getattr(config, "torch_dtype", None)
            )
        if configured_dtype is None:
            raise ValueError("dtype or element_bytes is required for KV cache estimation")
        element_bytes = bytes_per_element(str(configured_dtype))

    total = estimate_kv_cache_bytes(
        layers,
        kv_heads,
        head_dim,
        sequence_length,
        batch_size,
        element_bytes,
    )
    return KVCacheEstimate(
        num_hidden_layers=layers,
        num_key_value_heads=kv_heads,
        head_dim=head_dim,
        sequence_length=sequence_length,
        batch_size=batch_size,
        bytes_per_element=element_bytes,
        bytes=total,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Estimate decoder KV cache tensor size.")
    parser.add_argument("--model-config", required=True, help="Path to config.json")
    parser.add_argument("--sequence-length", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--dtype", default="bfloat16")
    return parser.parse_args()


def main() -> None:
    import json

    args = parse_args()
    with Path(args.model_config).open(encoding="utf-8") as handle:
        config = json.load(handle)
    estimate = estimate_from_model_config(
        config,
        sequence_length=args.sequence_length,
        batch_size=args.batch_size,
        dtype=args.dtype,
    )
    print(json.dumps(estimate.to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
