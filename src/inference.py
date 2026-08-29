from __future__ import annotations

import hashlib
import math
import statistics
import time
from dataclasses import asdict, dataclass

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from src.metrics import percentile


@dataclass
class RunResult:
    use_cache: bool
    prompt_tokens: int
    output_tokens: int
    tokenization_ms: float
    prefill_ms: float
    first_token_ms: float
    mean_tpot_ms: float
    p50_tpot_ms: float
    p95_tpot_ms: float
    total_generation_ms: float
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


def load_model(model_id: str, revision: str, dtype_name: str):
    if not torch.cuda.is_available():
        raise RuntimeError("A CUDA GPU is required for the Week 1 benchmark.")
    dtype = choose_dtype(dtype_name)
    tokenizer = AutoTokenizer.from_pretrained(model_id, revision=revision)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        revision=revision,
        torch_dtype=dtype,
        device_map={"": "cuda:0"},
    )
    model.eval()
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


def _measure_cuda(callable_):
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

    device_input = input_ids.to(model.device)
    prompt_tokens = device_input.shape[1]
    attention_mask = torch.ones_like(device_input)
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()

    prefill_output, prefill_ms = _measure_cuda(
        lambda: model(
            input_ids=device_input,
            attention_mask=attention_mask,
            use_cache=use_cache,
        )
    )
    next_token = prefill_output.logits[:, -1, :].argmax(dim=-1, keepdim=True)
    generated = [int(next_token.item())]
    decode_step_ms: list[float] = []
    past_key_values = prefill_output.past_key_values if use_cache else None
    full_sequence = torch.cat([device_input, next_token], dim=1)

    for _ in range(output_tokens - 1):
        if use_cache:
            step_mask = torch.ones(
                (1, prompt_tokens + len(generated)),
                dtype=attention_mask.dtype,
                device=model.device,
            )
            step_output, elapsed_ms = _measure_cuda(
                lambda: model(
                    input_ids=next_token,
                    attention_mask=step_mask,
                    past_key_values=past_key_values,
                    use_cache=True,
                )
            )
            past_key_values = step_output.past_key_values
        else:
            step_mask = torch.ones_like(full_sequence)
            step_output, elapsed_ms = _measure_cuda(
                lambda: model(
                    input_ids=full_sequence,
                    attention_mask=step_mask,
                    use_cache=False,
                )
            )

        next_token = step_output.logits[:, -1, :].argmax(dim=-1, keepdim=True)
        generated.append(int(next_token.item()))
        decode_step_ms.append(elapsed_ms)
        full_sequence = torch.cat([full_sequence, next_token], dim=1)

    total_ms = prefill_ms + sum(decode_step_ms)
    mean_tpot = statistics.fmean(decode_step_ms) if decode_step_ms else 0.0
    token_hash = hashlib.sha256(bytes(str(generated), "utf-8")).hexdigest()[:16]
    result = RunResult(
        use_cache=use_cache,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        tokenization_ms=tokenization_ms,
        prefill_ms=prefill_ms,
        first_token_ms=tokenization_ms + prefill_ms,
        mean_tpot_ms=mean_tpot,
        p50_tpot_ms=percentile(decode_step_ms, 0.50),
        p95_tpot_ms=percentile(decode_step_ms, 0.95),
        total_generation_ms=total_ms,
        output_tokens_per_second=output_tokens / (total_ms / 1_000),
        peak_memory_mb=torch.cuda.max_memory_allocated() / (1024**2),
        output_token_hash=token_hash,
    )
    return result, generated
