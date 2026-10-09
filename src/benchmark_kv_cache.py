from __future__ import annotations

import argparse
from pathlib import Path

import torch

from src.common import (
    load_yaml,
    read_json,
    require_clean_source,
    source_identity,
    utc_now,
    write_json,
)
from src.inference import (
    build_exact_length_input,
    load_model,
    run_greedy_generation,
    token_sequence_hash,
)
from src.result_store import append_row, read_rows
from src.week01_contract import (
    RESULT_FIELDS,
    collect_runtime_identity,
    create_run_metadata,
    expected_cases,
    validate_result_rows,
    validate_run_metadata,
    validate_model_snapshot,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark KV cache on versus off.")
    parser.add_argument("--config", default="configs/week01.yaml")
    return parser.parse_args()


def load_or_create_metadata(
    metadata_path: Path,
    output_path: Path,
    config: dict[str, object],
    source: dict[str, object],
    runtime: dict[str, object],
) -> dict[str, object]:
    if metadata_path.exists():
        metadata = read_json(metadata_path)
        validate_run_metadata(metadata, config, source=source, runtime=runtime)
        return metadata
    if output_path.exists() and output_path.stat().st_size:
        raise RuntimeError(
            f"Cannot resume {output_path}: required metadata {metadata_path} is missing."
        )
    metadata = create_run_metadata(config, source, runtime)
    write_json(metadata_path, metadata)
    return metadata


def main() -> None:
    args = parse_args()
    config = load_yaml(args.config)
    source = source_identity()
    require_clean_source(source)
    output_path = Path(config["output"]["raw_csv"])
    metadata_path = Path(config["output"]["run_metadata"])
    snapshot_path = Path(config["output"]["model_snapshot"])
    if not snapshot_path.is_file():
        raise RuntimeError(
            f"Missing model snapshot evidence {snapshot_path}; run make prepare-model first."
        )
    snapshot = read_json(snapshot_path)
    validate_model_snapshot(config, snapshot)
    runtime = collect_runtime_identity(config)
    metadata = load_or_create_metadata(
        metadata_path,
        output_path,
        config,
        source,
        runtime,
    )
    existing_rows = read_rows(output_path, expected_fields=RESULT_FIELDS)
    completed = validate_result_rows(
        existing_rows,
        metadata,
        config,
        require_complete=False,
    )
    expected = expected_cases(config)
    if completed == expected:
        validate_result_rows(existing_rows, metadata, config, require_complete=True)
        print(f"All {len(expected)} cases are complete and valid in {output_path}.")
        return
    if completed:
        print(
            f"Resuming run {metadata['run_id']}: "
            f"{len(completed)}/{len(expected)} cases complete."
        )

    torch.manual_seed(config["generation"]["seed"])
    tokenizer, model, dtype = load_model(
        config["model"]["id"],
        config["model"]["revision"],
        config["model"]["dtype"],
        local_files_only=bool(config["model"].get("local_files_only", True)),
        model_path=str(snapshot["snapshot_path"]),
    )
    resolved_dtype = str(dtype).removeprefix("torch.")
    if resolved_dtype != runtime["dtype"]:
        raise RuntimeError(
            f"Loaded dtype {resolved_dtype} does not match runtime contract {runtime['dtype']}"
        )
    parity_tokens = int(config["benchmark"]["parity_tokens"])
    if parity_tokens < 1:
        raise ValueError("benchmark.parity_tokens must be at least 1")

    inputs = {
        prompt_tokens: build_exact_length_input(
            tokenizer,
            config["generation"]["prompt"],
            prompt_tokens,
        )
        for prompt_tokens in config["benchmark"]["prompt_tokens"]
    }
    print("Warming every measured prompt/output shape and cache mode...")
    for prompt_tokens, (warmup_input, warmup_tokenization_ms) in inputs.items():
        for output_tokens in config["benchmark"]["output_tokens"]:
            for use_cache in config["benchmark"]["cache_modes"]:
                for _ in range(config["benchmark"]["warmup_runs"]):
                    run_greedy_generation(
                        model,
                        warmup_input,
                        output_tokens,
                        use_cache=use_cache,
                        tokenization_ms=warmup_tokenization_ms,
                    )
        print(f"warmed prompt_tokens={prompt_tokens}")

    reference_hashes: dict[tuple[int, int, int], str] = {}
    for row in existing_rows:
        key = (int(row["prompt_tokens"]), int(row["output_tokens"]), int(row["repeat"]))
        reference_hashes.setdefault(key, row["parity_token_hash"])

    written = 0
    for prompt_tokens in config["benchmark"]["prompt_tokens"]:
        for output_tokens in config["benchmark"]["output_tokens"]:
            for repeat in range(config["benchmark"]["repeats"]):
                for use_cache in config["benchmark"]["cache_modes"]:
                    current_case = (prompt_tokens, output_tokens, repeat, use_cache)
                    if current_case in completed:
                        print(
                            f"skip prompt={prompt_tokens:4d} output={output_tokens:3d} "
                            f"cache={str(use_cache):5s} repeat={repeat}"
                        )
                        continue
                    input_ids, tokenization_ms = build_exact_length_input(
                        tokenizer,
                        config["generation"]["prompt"],
                        prompt_tokens,
                    )
                    result, generated = run_greedy_generation(
                        model,
                        input_ids,
                        output_tokens,
                        use_cache=use_cache,
                        tokenization_ms=tokenization_ms,
                    )
                    parity_hash = token_sequence_hash(generated[:parity_tokens])
                    output_key = (prompt_tokens, output_tokens, repeat)
                    previous_hash = reference_hashes.setdefault(
                        output_key, parity_hash
                    )
                    if previous_hash != parity_hash:
                        raise RuntimeError(
                            "Cache on/off produced different tokens within the first "
                            f"{min(parity_tokens, output_tokens)} tokens for case {output_key}."
                        )
                    row = {
                        "timestamp": utc_now(),
                        "run_id": metadata["run_id"],
                        "git_commit": source["git_commit"],
                        "config_fingerprint": metadata["config_fingerprint"],
                        "runtime_fingerprint": metadata["runtime_fingerprint"],
                        "model": runtime["model"],
                        "model_revision": runtime["model_revision"],
                        "dtype": runtime["dtype"],
                        "repeat": repeat,
                        "parity_token_hash": parity_hash,
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

    rows = read_rows(output_path, expected_fields=RESULT_FIELDS)
    validate_result_rows(rows, metadata, config, require_complete=True)
    print(
        f"Wrote {written} new rows to {output_path}; "
        f"run {metadata['run_id']} is complete ({len(rows)}/{len(expected)} cases)."
    )


if __name__ == "__main__":
    main()
