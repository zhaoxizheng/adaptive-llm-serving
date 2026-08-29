from __future__ import annotations

import argparse
import json

import torch

from src.common import load_yaml
from src.inference import build_exact_length_input, load_model, run_greedy_generation


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one measured greedy generation.")
    parser.add_argument("--config", default="configs/week01.yaml")
    parser.add_argument("--prompt-tokens", type=int, default=32)
    parser.add_argument("--output-tokens", type=int, default=32)
    parser.add_argument("--no-cache", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_yaml(args.config)
    torch.manual_seed(config["generation"]["seed"])
    tokenizer, model, dtype = load_model(
        config["model"]["id"],
        config["model"]["revision"],
        config["model"]["dtype"],
    )
    input_ids, tokenization_ms = build_exact_length_input(
        tokenizer,
        config["generation"]["prompt"],
        args.prompt_tokens,
    )
    result, generated = run_greedy_generation(
        model,
        input_ids,
        args.output_tokens,
        use_cache=not args.no_cache,
        tokenization_ms=tokenization_ms,
    )
    print(json.dumps(result.to_dict(), indent=2))
    print("\nGenerated text:")
    print(tokenizer.decode(generated, skip_special_tokens=True))
    print(f"\nResolved dtype: {dtype}")


if __name__ == "__main__":
    main()

