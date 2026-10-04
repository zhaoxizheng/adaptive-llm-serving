from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.common import load_yaml, read_json, utc_now, write_json
from src.vllm_contract import config_fingerprint, validate_config
from src.week04_contract import (
    artifact_identity,
    load_run_metadata,
    server_attempt_identity,
    validate_artifact_identity,
)


def completion_payload(
    config: Mapping[str, object], *, stream: bool
) -> dict[str, object]:
    validated = validate_config(config)
    model = validated["model"]
    smoke = validated["smoke"]
    payload: dict[str, object] = {
        "model": model["served_model_name"],
        "prompt": smoke["prompt"],
        "max_tokens": smoke["requested_output_tokens"],
        "temperature": smoke["temperature"],
        "seed": smoke["seed"],
        "stream": stream,
        "ignore_eos": True,
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}
    return payload


def _choice(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    choices = payload.get("choices")
    if (
        not isinstance(choices, list)
        or len(choices) != 1
        or not isinstance(choices[0], Mapping)
    ):
        raise ValueError("OpenAI response must contain exactly one choice")
    return choices[0]


def _usage(payload: Mapping[str, Any]) -> tuple[int | None, int | None]:
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return None, None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return (
        int(prompt) if prompt is not None else None,
        int(completion) if completion is not None else None,
    )


def run_nonstreaming(
    config: Mapping[str, object],
    *,
    api_key: str | None = None,
    metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    from src.openai_stream import post_json

    validated = validate_config(config)
    server = validated["server"]
    model = validated["model"]
    smoke = validated["smoke"]
    url = f"http://127.0.0.1:{server['port']}/v1/completions"
    started = time.perf_counter()
    response = post_json(
        url,
        completion_payload(config, stream=False),
        api_key=api_key,
        timeout=float(smoke["timeout_seconds"]),
    )
    elapsed_ms = (time.perf_counter() - started) * 1_000
    if not isinstance(response, Mapping):
        raise ValueError("OpenAI response must be a JSON object")
    if response.get("model") not in {model["served_model_name"], model["id"]}:
        raise ValueError("OpenAI response model does not match the configured model")
    choice = _choice(response)
    text = choice.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("Non-streaming response contains no generated text")
    prompt_tokens, output_tokens = _usage(response)
    if output_tokens is not None and output_tokens < 1:
        raise ValueError("Non-streaming response reports zero output tokens")
    payload = {
        "schema_version": 1,
        "status": "completed",
        "captured_at": utc_now(),
        "path": "non-streaming",
        "config_fingerprint": config_fingerprint(config),
        "model": response.get("model"),
        "model_revision": model["revision"],
        "requested_output_tokens": smoke["requested_output_tokens"],
        "actual_prompt_tokens": prompt_tokens,
        "actual_output_tokens": output_tokens,
        "finish_reason": choice.get("finish_reason"),
        "text": text,
        "end_to_end_ms": elapsed_ms,
    }
    if metadata is not None:
        payload.update(artifact_identity(metadata))
    return payload


def _iter_stream(config: Mapping[str, object], api_key: str | None) -> Iterable[object]:
    from src.openai_stream import stream_openai

    validated = validate_config(config)
    server = validated["server"]
    smoke = validated["smoke"]
    url = f"http://127.0.0.1:{server['port']}/v1/completions"
    return stream_openai(
        url,
        completion_payload(config, stream=True),
        api_key=api_key,
        timeout=float(smoke["timeout_seconds"]),
        max_duration=float(smoke["timeout_seconds"]),
    )


def run_streaming(
    config: Mapping[str, object],
    *,
    api_key: str | None = None,
    metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    validated = validate_config(config)
    model = validated["model"]
    smoke = validated["smoke"]
    started = time.perf_counter()
    first_content_at: float | None = None
    content: list[str] = []
    finish_reason: str | None = None
    usage: Mapping[str, Any] | None = None
    done_seen = False
    event_count = 0
    for chunk in _iter_stream(config, api_key):
        event_count += 1
        if getattr(chunk, "done", False):
            done_seen = True
            continue
        chunk_usage = getattr(chunk, "usage", None)
        if isinstance(chunk_usage, Mapping):
            usage = chunk_usage
        for choice in getattr(chunk, "choices", ()):
            piece = getattr(choice, "content", None)
            if piece is not None:
                if first_content_at is None:
                    first_content_at = time.perf_counter()
                content.append(piece)
            if getattr(choice, "finish_reason", None) is not None:
                finish_reason = str(choice.finish_reason)
    ended = time.perf_counter()
    if not done_seen:
        raise ValueError("Streaming response ended without [DONE]")
    text = "".join(content)
    if first_content_at is None or not text:
        raise ValueError("Streaming response contains no generated content")
    prompt_tokens = (
        int(usage["prompt_tokens"])
        if usage and usage.get("prompt_tokens") is not None
        else None
    )
    output_tokens = (
        int(usage["completion_tokens"])
        if usage and usage.get("completion_tokens") is not None
        else None
    )
    payload = {
        "schema_version": 1,
        "status": "completed",
        "captured_at": utc_now(),
        "path": "streaming",
        "config_fingerprint": config_fingerprint(config),
        "model": model["served_model_name"],
        "model_revision": model["revision"],
        "requested_output_tokens": smoke["requested_output_tokens"],
        "actual_prompt_tokens": prompt_tokens,
        "actual_output_tokens": output_tokens,
        "finish_reason": finish_reason,
        "text": text,
        "event_count": event_count,
        "ttft_ms": (first_content_at - started) * 1_000,
        "end_to_end_ms": (ended - started) * 1_000,
    }
    if metadata is not None:
        payload.update(artifact_identity(metadata))
    return payload


def smoke_plan(config: Mapping[str, object]) -> dict[str, object]:
    validated = validate_config(config)
    return {
        "url": f"http://127.0.0.1:{validated['server']['port']}/v1/completions",
        "nonstreaming_payload": completion_payload(config, stream=False),
        "streaming_payload": completion_payload(config, stream=True),
        "timeout_seconds": validated["smoke"]["timeout_seconds"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Week 4 OpenAI-compatible smoke tests."
    )
    parser.add_argument("--config", default="configs/week04.yaml")
    parser.add_argument(
        "--mode", choices=("nonstreaming", "streaming", "both"), default="both"
    )
    parser.add_argument("--api-key")
    parser.add_argument("--plan", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_yaml(args.config)
    if args.plan:
        print(json.dumps(smoke_plan(config), indent=2, sort_keys=True))
        return
    output = config["output"]
    if not isinstance(output, Mapping):
        raise ValueError("output must be a mapping")
    metadata = load_run_metadata(config)
    server_path = Path(str(config["server"]["pid_metadata_path"]))
    server = read_json(server_path)
    if not isinstance(server, Mapping):
        raise ValueError("server process metadata must be a JSON object")
    validate_artifact_identity(server, metadata, "server process metadata")
    if server.get("state") != "ready":
        raise ValueError("server process metadata is not ready")
    server_identity = server_attempt_identity(server, "server process metadata")
    if args.mode in {"nonstreaming", "both"}:
        path = Path(str(output["nonstream_smoke_json"]))
        payload = run_nonstreaming(config, api_key=args.api_key, metadata=metadata)
        payload.update(server_identity)
        write_json(path, payload)
        print(f"Non-streaming smoke passed; wrote {path}")
    if args.mode in {"streaming", "both"}:
        path = Path(str(output["stream_smoke_json"]))
        payload = run_streaming(config, api_key=args.api_key, metadata=metadata)
        payload.update(server_identity)
        write_json(path, payload)
        print(f"Streaming smoke passed; wrote {path}")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"OpenAI smoke failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
