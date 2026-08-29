from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path

import torch
import transformers

from src.common import git_commit, load_yaml, utc_now, write_json
from src.inference import build_exact_length_input, load_model, run_greedy_generation
from src.result_store import append_row, case_key, read_rows


RESULT_FIELDS = [
    "timestamp",
    "git_commit",
    "config_fingerprint",
    "model",
    "dtype",
    "repeat",
    "use_cache",
    "prompt_tokens",
    "output_tokens",
    "tokenization_ms",
    "prefill_ms",
    "first_token_ms",
    "mean_tpot_ms",
    "p50_tpot_ms",
    "p95_tpot_ms",
    "total_generation_ms",
    "output_tokens_per_second",
    "peak_memory_mb",
    "output_token_hash",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark KV cache on versus off.")
    parser.add_argument("--config", default="configs/week01.yaml")
    return parser.parse_args()


def config_fingerprint(config: dict[str, object]) -> str:
    serialized = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]


def main() -> None:
    args = parse_args()
    config = load_yaml(args.config)
    benchmark = config["benchmark"]
    output_path = Path(config["output"]["raw_csv"])
    fingerprint = config_fingerprint(config)
    existing_rows = read_rows(output_path)
    completed = {case_key(row) for row in existing_rows}
    expected = {
        (prompt_tokens, output_tokens, repeat, use_cache)
        for prompt_tokens in benchmark["prompt_tokens"]
        for output_tokens in benchmark["output_tokens"]
        for repeat in range(benchmark["repeats"])
        for use_cache in benchmark["cache_modes"]
    }

    existing_fingerprints = {row.get("config_fingerprint", "") for row in existing_rows}
    if existing_rows and existing_fingerprints != {fingerprint}:
        raise RuntimeError(
            f"Cannot resume {output_path}: its configuration differs from {args.config}. "
            "Move the existing CSV to a separate run directory before starting a new matrix."
        )
    unexpected = completed.difference(expected)
    if unexpected:
        raise RuntimeError(f"Existing result file contains cases outside this matrix: {unexpected}")
    if completed == expected:
        print(f"All {len(expected)} cases are already complete in {output_path}.")
        return
    if completed:
        print(f"Resuming {output_path}: {len(completed)}/{len(expected)} cases complete.")

    torch.manual_seed(config["generation"]["seed"])

    tokenizer, model, dtype = load_model(
        config["model"]["id"],
        config["model"]["revision"],
        config["model"]["dtype"],
    )

    metadata = {
        "started_at": utc_now(),
        "git_commit": git_commit(),
        "platform": platform.platform(),
        "gpu": torch.cuda.get_device_name(0),
        "gpu_count": torch.cuda.device_count(),
        "cuda": torch.version.cuda,
        "pytorch": torch.__version__,
        "transformers": transformers.__version__,
        "model": config["model"]["id"],
        "revision": config["model"]["revision"],
        "dtype": str(dtype),
        "config_fingerprint": fingerprint,
        "resumed_completed_cases": len(completed),
        "total_cases": len(expected),
        "config": config,
    }
    write_json(config["output"]["run_metadata"], metadata)

    inputs = {}
    for prompt_tokens in benchmark["prompt_tokens"]:
        inputs[prompt_tokens] = build_exact_length_input(
            tokenizer, config["generation"]["prompt"], prompt_tokens
        )

    max_prompt = max(benchmark["prompt_tokens"])
    warmup_input, warmup_tokenization_ms = inputs[max_prompt]
    print(f"Warming up with {max_prompt} prompt tokens...")
    for use_cache in benchmark["cache_modes"]:
        for _ in range(benchmark["warmup_runs"]):
            run_greedy_generation(
                model,
                warmup_input,
                min(8, min(benchmark["output_tokens"])),
                use_cache=use_cache,
                tokenization_ms=warmup_tokenization_ms,
            )

    reference_hashes: dict[tuple[int, int, int], str] = {}
    for row in existing_rows:
        reference_key = (int(row["prompt_tokens"]), int(row["output_tokens"]), int(row["repeat"]))
        previous_hash = reference_hashes.setdefault(reference_key, row["output_token_hash"])
        if previous_hash != row["output_token_hash"]:
            raise RuntimeError(f"Existing cache on/off results disagree for case {reference_key}.")

    written = 0
    for prompt_tokens in benchmark["prompt_tokens"]:
        input_ids, tokenization_ms = inputs[prompt_tokens]
        for output_tokens in benchmark["output_tokens"]:
            for repeat in range(benchmark["repeats"]):
                for use_cache in benchmark["cache_modes"]:
                    current_case = (prompt_tokens, output_tokens, repeat, use_cache)
                    if current_case in completed:
                        print(
                            f"skip prompt={prompt_tokens:4d} output={output_tokens:3d} "
                            f"cache={str(use_cache):5s} repeat={repeat}"
                        )
                        continue
                    result, _ = run_greedy_generation(
                        model,
                        input_ids,
                        output_tokens,
                        use_cache=use_cache,
                        tokenization_ms=tokenization_ms,
                    )
                    key = (prompt_tokens, output_tokens, repeat)
                    if (
                        key in reference_hashes
                        and reference_hashes[key] != result.output_token_hash
                    ):
                        raise RuntimeError(
                            f"Cache on/off produced different tokens for case {key}."
                        )
                    reference_hashes[key] = result.output_token_hash
                    row = {
                        "timestamp": utc_now(),
                        "git_commit": metadata["git_commit"],
                        "config_fingerprint": fingerprint,
                        "model": metadata["model"],
                        "dtype": metadata["dtype"],
                        "repeat": repeat,
                        **result.to_dict(),
                    }
                    append_row(output_path, row, RESULT_FIELDS)
                    completed.add(current_case)
                    written += 1
                    print(
                        f"prompt={prompt_tokens:4d} output={output_tokens:3d} "
                        f"cache={str(use_cache):5s} repeat={repeat} "
                        f"total_ms={result.total_generation_ms:.2f} "
                        f"tok/s={result.output_tokens_per_second:.2f}"
                    )

    print(
        f"Wrote {written} new rows to {output_path}; "
        f"{len(completed)}/{len(expected)} cases complete."
    )


if __name__ == "__main__":
    main()

