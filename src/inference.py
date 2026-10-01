from __future__ import annotations

import hashlib
import math
import time
from dataclasses import asdict, dataclass
from typing import Callable, TypeVar

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from src.latency import summarize_latency

T = TypeVar("T")


@dataclass
class RunResult:
    use_cache: bool
    prompt_tokens: int
    output_tokens: int
    tokenization_ms: float
    h2d_ms: float
    prefill_forward_ms: float
    first_token_selection_ms: float
    inference_ttft_ms: float
    end_to_end_ttft_ms: float
    decode_ms: float
    mean_tpot_ms: float | None
    p50_tpot_ms: float | None
    p95_tpot_ms: float | None
    total_generation_ms: float
    end_to_end_ms: float
    output_tokens_per_second: float
    peak_memory_mb: float
    output_token_hash: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def choose_dtype(name: str) -> torch.dtype:
    if name == "auto":
        return torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    choices = {"bfloat16": torch.bfloat16, "float16": torch.float16}
    if name not in choices:
        raise ValueError(f"Unsupported dtype: {name}")
    return choices[name]


def load_model(
    model_id: str,
    revision: str,
    dtype_name: str,
    *,
    local_files_only: bool,
    model_path: str | None = None,
):
    if not torch.cuda.is_available():
        raise RuntimeError("A CUDA GPU is required for the Week 1 benchmark.")
    dtype = choose_dtype(dtype_name)
    source = model_path or model_id
    tokenizer = AutoTokenizer.from_pretrained(
        source,
        revision=None if model_path else revision,
        local_files_only=local_files_only,
    )
    model = AutoModelForCausalLM.from_pretrained(
        source,
        revision=None if model_path else revision,
        torch_dtype=dtype,
        device_map={"": "cuda:0"},
        local_files_only=local_files_only,
    )
    model.eval()
    resolved_revisions = {
        value
        for value in (
            getattr(tokenizer, "init_kwargs", {}).get("_commit_hash"),
            getattr(model.config, "_commit_hash", None),
        )
        if value
    }
    if not model_path and resolved_revisions and resolved_revisions != {revision}:
        raise RuntimeError(
            f"Resolved model revisions {sorted(resolved_revisions)} do not match {revision}."
        )
    return tokenizer, model, dtype


def build_exact_length_input(
    tokenizer, prompt: str, target_tokens: int
) -> tuple[torch.Tensor, float]:
    started = time.perf_counter()
    encoded = tokenizer(prompt, add_special_tokens=True, return_tensors="pt").input_ids[0]
    if encoded.numel() == 0:
        raise ValueError("Prompt tokenization produced no tokens.")
    repeats = math.ceil(target_tokens / encoded.numel())
    token_ids = encoded.repeat(repeats)[:target_tokens].unsqueeze(0)
    tokenization_ms = (time.perf_counter() - started) * 1_000
    return token_ids, tokenization_ms


def _measure_cuda(callable_: Callable[[], T]) -> tuple[T, float]:
    torch.cuda.synchronize()
    started = time.perf_counter()
    value = callable_()
    torch.cuda.synchronize()
    return value, (time.perf_counter() - started) * 1_000


@torch.inference_mode()
def run_greedy_generation(
    model,
    input_ids: torch.Tensor,
    output_tokens: int,
    use_cache: bool,
    tokenization_ms: float = 0.0,
) -> tuple[RunResult, list[int]]:
    if output_tokens < 1:
        raise ValueError("output_tokens must be at least 1")

    prompt_tokens = input_ids.shape[1]
    host_attention_mask = torch.ones_like(input_ids)
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()

    (device_input, attention_mask), h2d_ms = _measure_cuda(
        lambda: (input_ids.to(model.device), host_attention_mask.to(model.device))
    )
    prefill_output, prefill_forward_ms = _measure_cuda(
        lambda: model(
            input_ids=device_input,
            attention_mask=attention_mask,
            use_cache=use_cache,
        )
    )

    def select_first_token() -> tuple[torch.Tensor, int]:
        token = prefill_output.logits[:, -1, :].argmax(dim=-1, keepdim=True)
        return token, int(token.item())

    (next_token, first_token_id), first_token_selection_ms = _measure_cuda(
        select_first_token
    )
    generated = [first_token_id]
    decode_step_ms: list[float] = []
    past_key_values = prefill_output.past_key_values if use_cache else None
    full_sequence = None

    for _ in range(output_tokens - 1):

        def decode_step() -> tuple[torch.Tensor, int]:
            nonlocal full_sequence, past_key_values
            if use_cache:
                step_mask = torch.ones(
                    (1, prompt_tokens + len(generated)),
                    dtype=attention_mask.dtype,
                    device=model.device,
                )
                step_output = model(
                    input_ids=next_token,
                    attention_mask=step_mask,
                    past_key_values=past_key_values,
                    use_cache=True,
                )
                past_key_values = step_output.past_key_values
            else:
                if full_sequence is None:
                    full_sequence = torch.cat([device_input, next_token], dim=1)
                step_mask = torch.ones_like(full_sequence)
                step_output = model(
                    input_ids=full_sequence,
                    attention_mask=step_mask,
                    use_cache=False,
                )
            token = step_output.logits[:, -1, :].argmax(dim=-1, keepdim=True)
            token_id = int(token.item())
            if not use_cache:
                full_sequence = torch.cat([full_sequence, token], dim=1)
            return token, token_id

        (next_token, token_id), elapsed_ms = _measure_cuda(decode_step)
        generated.append(token_id)
        decode_step_ms.append(elapsed_ms)

    timing = summarize_latency(
        tokenization_ms=tokenization_ms,
        h2d_ms=h2d_ms,
        prefill_forward_ms=prefill_forward_ms,
        first_token_selection_ms=first_token_selection_ms,
        decode_step_ms=decode_step_ms,
        output_tokens=output_tokens,
    )
    token_hash = hashlib.sha256(bytes(str(generated), "utf-8")).hexdigest()[:16]
    result = RunResult(
        use_cache=use_cache,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        tokenization_ms=tokenization_ms,
        h2d_ms=h2d_ms,
        prefill_forward_ms=prefill_forward_ms,
        first_token_selection_ms=first_token_selection_ms,
        inference_ttft_ms=float(timing["inference_ttft_ms"]),
        end_to_end_ttft_ms=float(timing["end_to_end_ttft_ms"]),
        decode_ms=float(timing["decode_ms"]),
        mean_tpot_ms=timing["mean_tpot_ms"],
        p50_tpot_ms=timing["p50_tpot_ms"],
        p95_tpot_ms=timing["p95_tpot_ms"],
        total_generation_ms=float(timing["total_generation_ms"]),
        end_to_end_ms=float(timing["end_to_end_ms"]),
        output_tokens_per_second=float(timing["output_tokens_per_second"]),
        peak_memory_mb=torch.cuda.max_memory_allocated() / (1024**2),
        output_token_hash=token_hash,
    )
    return result, generated
