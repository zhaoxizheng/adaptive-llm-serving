from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Mapping

from src.common import load_yaml, utc_now, write_json
from src.vllm_contract import PINNED_VLLM_VERSION, config_fingerprint, validate_config
from src.week04_contract import artifact_identity, load_run_metadata


def offline_plan(config: Mapping[str, object]) -> dict[str, object]:
    validated = validate_config(config)
    model = validated["model"]
    server = validated["server"]
    smoke = validated["smoke"]
    return {
        "model": model["id"],
        "revision": model["revision"],
        "dtype": model["dtype"],
        "max_model_len": server["max_model_len"],
        "gpu_memory_utilization": server["gpu_memory_utilization"],
        "prompt": smoke["prompt"],
        "requested_output_tokens": smoke["requested_output_tokens"],
        "temperature": smoke["temperature"],
        "seed": smoke["seed"],
        "vllm_version_required": PINNED_VLLM_VERSION,
        "gpu_compatibility": "unverified_until_smoke_succeeds",
    }


def _installed_vllm_version() -> str:
    try:
        installed = version("vllm")
    except PackageNotFoundError as error:
        raise RuntimeError(
            "vLLM is not installed. Use .venv-vllm and requirements-vllm.txt."
        ) from error
    if installed != PINNED_VLLM_VERSION:
        raise RuntimeError(
            f"Expected vLLM {PINNED_VLLM_VERSION}, found {installed}; "
            "refusing to create incomparable smoke evidence."
        )
    return installed


def run_offline_smoke(
    config: Mapping[str, object], metadata: Mapping[str, object] | None = None
) -> dict[str, object]:
    plan = offline_plan(config)
    installed = _installed_vllm_version()
    # vLLM and its PyTorch dependency are intentionally imported only on the GPU path.
    try:
        from vllm import LLM, SamplingParams
    except ImportError as error:
        raise RuntimeError("The pinned vLLM runtime could not be imported") from error

    started = time.perf_counter()
    llm = LLM(
        model=str(plan["model"]),
        revision=str(plan["revision"]),
        dtype=str(plan["dtype"]),
        max_model_len=int(plan["max_model_len"]),
        gpu_memory_utilization=float(plan["gpu_memory_utilization"]),
    )
    sampling = SamplingParams(
        temperature=float(plan["temperature"]),
        max_tokens=int(plan["requested_output_tokens"]),
        ignore_eos=True,
        seed=int(plan["seed"]),
    )
    outputs = llm.generate([str(plan["prompt"])], sampling, use_tqdm=False)
    elapsed_ms = (time.perf_counter() - started) * 1_000
    if len(outputs) != 1 or len(outputs[0].outputs) != 1:
        raise RuntimeError(
            "Offline smoke expected exactly one request and one completion"
        )
    request = outputs[0]
    completion = request.outputs[0]
    output_token_ids = list(completion.token_ids)
    if not output_token_ids:
        raise RuntimeError("Offline smoke returned no output tokens")
    prompt_token_ids = list(request.prompt_token_ids or [])
    payload = {
        "schema_version": 1,
        "status": "completed",
        "captured_at": utc_now(),
        "config_fingerprint": config_fingerprint(config),
        "vllm_version": installed,
        "python": platform.python_version(),
        "gpu_compatibility": "verified_by_offline_smoke",
        "model": plan["model"],
        "model_revision": plan["revision"],
        "requested_output_tokens": plan["requested_output_tokens"],
        "actual_prompt_tokens": len(prompt_token_ids),
        "actual_output_tokens": len(output_token_ids),
        "finish_reason": completion.finish_reason,
        "generated_text": completion.text,
        "elapsed_ms_including_engine_startup": elapsed_ms,
        "steady_state_latency_claimed": False,
    }
    if metadata is not None:
        payload.update(artifact_identity(metadata))
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Week 4 offline vLLM smoke test."
    )
    parser.add_argument("--config", default="configs/week04.yaml")
    parser.add_argument("--output")
    parser.add_argument("--plan", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_yaml(args.config)
    if args.plan:
        print(json.dumps(offline_plan(config), indent=2, sort_keys=True))
        return
    metadata = load_run_metadata(config)
    payload = run_offline_smoke(config, metadata)
    output = _mapping_output(config, args.output)
    write_json(output, payload)
    print(f"Offline vLLM smoke passed; wrote {output}")


def _mapping_output(config: Mapping[str, object], override: str | None) -> Path:
    if override:
        return Path(override)
    output = config.get("output")
    if not isinstance(output, Mapping):
        raise ValueError("output must be a mapping")
    return Path(str(output["offline_smoke_json"]))


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"Offline smoke failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
