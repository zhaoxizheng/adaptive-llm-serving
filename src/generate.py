from __future__ import annotations

import argparse
import json
import uuid
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
from src.inference import build_exact_length_input, load_model, run_greedy_generation
from src.week01_contract import (
    collect_runtime_identity,
    config_fingerprint,
    runtime_fingerprint,
    validate_model_snapshot,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one measured greedy generation.")
    parser.add_argument("--config", default="configs/week01.yaml")
    parser.add_argument("--prompt-tokens", type=int, default=32)
    parser.add_argument("--output-tokens", type=int, default=32)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--output")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_yaml(args.config)
    source = source_identity()
    require_clean_source(source)
    snapshot = read_json(config["output"]["model_snapshot"])
    validate_model_snapshot(config, snapshot)
    runtime = collect_runtime_identity(config)
    torch.manual_seed(config["generation"]["seed"])
    tokenizer, model, dtype = load_model(
        config["model"]["id"],
        config["model"]["revision"],
        config["model"]["dtype"],
        local_files_only=bool(config["model"].get("local_files_only", True)),
        model_path=str(snapshot["snapshot_path"]),
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
    generated_text = tokenizer.decode(generated, skip_special_tokens=True)
    payload = {
        "schema_version": 1,
        "smoke_id": str(uuid.uuid4()),
        "status": "completed",
        "captured_at": utc_now(),
        "source": source_identity(),
        "config_fingerprint": config_fingerprint(config),
        "runtime_fingerprint": runtime_fingerprint(runtime),
        "runtime": runtime,
        "model": config["model"]["id"],
        "model_revision": config["model"]["revision"],
        "resolved_dtype": str(dtype).removeprefix("torch."),
        "metrics": result.to_dict(),
        "generated_token_ids": generated,
        "generated_text": generated_text,
    }
    output = Path(args.output or config["output"]["smoke_json"])
    write_json(output, payload)
    print(json.dumps(payload, indent=2))
    print(f"\nSmoke evidence written to {output}")


if __name__ == "__main__":
    main()

