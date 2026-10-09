from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Sequence, TypeVar

from src.batch_inference import (
    build_exact_length_batch,
    build_static_batch,
    exact_length_token_ids,
    summarize_batch_latency,
)
from src.week02_smoke import generated_tokens_sha256

T = TypeVar("T")
MIB = 1024**2


@dataclass(frozen=True)
class CudaMemory:
    allocated_bytes: int
    reserved_bytes: int


@dataclass(frozen=True)
class BatchRunResult:
    batch_size: int
    prompt_tokens: int
    output_tokens: int
    use_cache: bool
    actual_output_tokens: int
    preprocessing_ms: float
    h2d_ms: float
    gpu_ttft_ms: float
    e2e_ttft_ms: float
    mean_tpot_ms: float | None
    p95_itl_ms: float | None
    generation_ms: float
    e2e_latency_ms: float
    output_tokens_per_second: float
    requests_per_second: float
    model_baseline_allocated_bytes: int
    model_baseline_reserved_bytes: int
    peak_memory_allocated_bytes: int
    peak_memory_reserved_bytes: int
    memory_allocated_delta_bytes: int
    memory_reserved_delta_bytes: int
    model_baseline_allocated_mb: float
    model_baseline_reserved_mb: float
    peak_memory_allocated_mb: float
    peak_memory_reserved_mb: float
    memory_allocated_delta_mb: float
    memory_reserved_delta_mb: float
    theoretical_kv_cache_bytes: int
    theoretical_kv_cache_mib: float
    output_token_hash: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _torch():
    try:
        import torch
    except ImportError as error:
        raise RuntimeError("PyTorch is required only for the GPU benchmark backend") from error
    return torch


def choose_dtype(name: str):
    torch = _torch()
    if name == "auto":
        return torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    choices = {"bfloat16": torch.bfloat16, "float16": torch.float16}
    try:
        return choices[name]
    except KeyError as error:
        raise ValueError(f"Unsupported dtype: {name}") from error


def load_hf_batch_model(
    model_id: str,
    revision: str,
    dtype_name: str,
    *,
    local_files_only: bool,
    model_path: str | None = None,
):
    """Load the pinned tokenizer/model without importing GPU libraries at module import."""

    torch = _torch()
    if not torch.cuda.is_available():
        raise RuntimeError("A CUDA GPU is required for the Week 2 benchmark.")
    try:
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as error:
        raise RuntimeError("Transformers is required for the Week 2 GPU benchmark") from error

    dtype = choose_dtype(dtype_name)
    source = model_path or model_id
    tokenizer = AutoTokenizer.from_pretrained(
        source,
        revision=None if model_path else revision,
        local_files_only=local_files_only,
    )
    if tokenizer.pad_token_id is None:
        if tokenizer.eos_token_id is None:
            raise RuntimeError("Tokenizer has neither a pad token nor an EOS fallback")
        tokenizer.pad_token = tokenizer.eos_token
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


def prepare_batch_tensors(
    tokenizer: Any,
    prompt: str,
    *,
    batch_size: int,
    prompt_tokens: int,
    padding_side: str = "left",
):
    """统一计时分词、padding、掩码和 CPU 张量构造；返回形状相同的输入与掩码。"""

    torch = _torch()
    if tokenizer.pad_token_id is None:
        raise ValueError("tokenizer.pad_token_id must be configured")
    started = time.perf_counter()
    batch = build_exact_length_batch(
        tokenizer,
        prompt,
        batch_size=batch_size,
        target_tokens=prompt_tokens,
        pad_token_id=int(tokenizer.pad_token_id),
        padding_side=padding_side,
    )
    input_ids = torch.tensor(batch.input_ids, dtype=torch.long)
    attention_mask = torch.tensor(batch.attention_mask, dtype=torch.long)
    preprocessing_ms = (time.perf_counter() - started) * 1_000
    if tuple(input_ids.shape) != (batch_size, prompt_tokens):
        raise RuntimeError("Static batch input shape differs from the controlled workload")
    if input_ids.shape != attention_mask.shape:
        raise RuntimeError("input_ids and attention_mask shapes differ")
    return input_ids, attention_mask, preprocessing_ms


def deterministic_request_token_ids(
    tokenizer: Any, request_id: str, base_prompt: str, prompt_tokens: int
) -> list[int]:
    """Construct policy-independent exact-length tokens for one trace request."""

    if not request_id:
        raise ValueError("request_id must not be blank")
    return exact_length_token_ids(
        tokenizer, f"Trace request {request_id}. {base_prompt}", prompt_tokens
    )


def prepare_trace_batch_tensors(
    tokenizer: Any,
    requests: Sequence[object],
    base_prompt: str,
    *,
    padding_side: str = "left",
):
    """根据请求身份构造确定性输入，使同一请求在不同组批策略下使用相同 token。"""

    torch = _torch()
    if tokenizer.pad_token_id is None:
        raise ValueError("tokenizer.pad_token_id must be configured")
    if not requests:
        raise ValueError("requests must contain at least one trace request")
    started = time.perf_counter()
    sequences: list[list[int]] = []
    target_lengths: set[int] = set()
    for request in requests:
        request_id = str(getattr(request, "request_id"))
        prompt_tokens = int(getattr(request, "prompt_tokens"))
        target_lengths.add(prompt_tokens)
        sequences.append(
            deterministic_request_token_ids(
                tokenizer, request_id, base_prompt, prompt_tokens
            )
        )
    if len(target_lengths) != 1:
        raise ValueError("Week 3 HF batches require one fixed prompt-token shape")
    target_tokens = next(iter(target_lengths))
    batch = build_static_batch(
        sequences,
        int(tokenizer.pad_token_id),
        padding_side=padding_side,
        padded_length=target_tokens,
    )
    input_ids = torch.tensor(batch.input_ids, dtype=torch.long)
    attention_mask = torch.tensor(batch.attention_mask, dtype=torch.long)
    preprocessing_ms = (time.perf_counter() - started) * 1_000
    return input_ids, attention_mask, preprocessing_ms


def capture_model_memory_baseline() -> CudaMemory:
    torch = _torch()
    torch.cuda.synchronize()
    torch.cuda.empty_cache()
    return CudaMemory(
        allocated_bytes=int(torch.cuda.memory_allocated()),
        reserved_bytes=int(torch.cuda.memory_reserved()),
    )


def current_peak_memory() -> CudaMemory:
    torch = _torch()
    return CudaMemory(
        allocated_bytes=int(torch.cuda.max_memory_allocated()),
        reserved_bytes=int(torch.cuda.max_memory_reserved()),
    )


def memory_fields(
    baseline: CudaMemory,
    peak: CudaMemory,
    theoretical_kv_cache_bytes: int,
) -> dict[str, int | float]:
    allocated_delta = max(0, peak.allocated_bytes - baseline.allocated_bytes)
    reserved_delta = max(0, peak.reserved_bytes - baseline.reserved_bytes)
    return {
        "model_baseline_allocated_bytes": baseline.allocated_bytes,
        "model_baseline_reserved_bytes": baseline.reserved_bytes,
        "peak_memory_allocated_bytes": peak.allocated_bytes,
        "peak_memory_reserved_bytes": peak.reserved_bytes,
        "memory_allocated_delta_bytes": allocated_delta,
        "memory_reserved_delta_bytes": reserved_delta,
        "model_baseline_allocated_mb": baseline.allocated_bytes / MIB,
        "model_baseline_reserved_mb": baseline.reserved_bytes / MIB,
        "peak_memory_allocated_mb": peak.allocated_bytes / MIB,
        "peak_memory_reserved_mb": peak.reserved_bytes / MIB,
        "memory_allocated_delta_mb": allocated_delta / MIB,
        "memory_reserved_delta_mb": reserved_delta / MIB,
        "theoretical_kv_cache_bytes": theoretical_kv_cache_bytes,
        "theoretical_kv_cache_mib": theoretical_kv_cache_bytes / MIB,
    }


def _measure_cuda(callable_: Callable[[], T]) -> tuple[T, float]:
    torch = _torch()
    torch.cuda.synchronize()
    started = time.perf_counter()
    value = callable_()
    torch.cuda.synchronize()
    return value, (time.perf_counter() - started) * 1_000


def _select_prompt_tokens(logits: Any, attention_mask: Any):
    """Select one next token per row at that row's last non-padding position."""

    torch = _torch()
    positions = torch.arange(attention_mask.shape[1], device=attention_mask.device)
    # 每行定位最后一个非 padding 位置，兼容左/右 padding，不能统一取最后一列。
    last_positions = (attention_mask.to(dtype=torch.long) * positions).max(dim=1).values
    row_positions = torch.arange(logits.shape[0], device=logits.device)
    last_logits = logits[row_positions, last_positions, :]
    return last_logits.argmax(dim=-1, keepdim=True)


def is_cuda_oom(error: BaseException) -> bool:
    torch = _torch()
    oom_type = getattr(torch.cuda, "OutOfMemoryError", None)
    typed_oom = oom_type is not None and isinstance(error, oom_type)
    return typed_oom or "out of memory" in str(error).lower()


def cleanup_cuda_after_failure() -> None:
    torch = _torch()
    try:
        torch.cuda.synchronize()
    except Exception:
        pass
    torch.cuda.empty_cache()


def run_batched_greedy_generation(
    model: Any,
    host_input_ids: Any,
    host_attention_mask: Any,
    *,
    output_tokens: int,
    preprocessing_ms: float,
    baseline: CudaMemory,
    theoretical_kv_cache_bytes: int,
) -> tuple[BatchRunResult, list[list[int]]]:
    """整批执行开启 KV Cache 的定长贪心生成，返回性能指标和每行生成的 token。

    每行生成 output_tokens 个 token，不因 EOS 提前停止；模型加载不计入生成耗时。
    """

    torch = _torch()
    if output_tokens < 1:
        raise ValueError("output_tokens must be at least 1")
    if host_input_ids.ndim != 2 or host_input_ids.shape != host_attention_mask.shape:
        raise ValueError("input_ids and attention_mask must be equal-shape rank-2 tensors")
    batch_size, prompt_tokens = map(int, host_input_ids.shape)
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()

    # 禁用梯度记录；同步 GPU 后分别计量输入传输和预填充。
    with torch.inference_mode():
        (device_input_ids, attention_mask), h2d_ms = _measure_cuda(
            lambda: (
                host_input_ids.to(model.device),
                host_attention_mask.to(model.device),
            )
        )
        prefill_output, gpu_ttft_ms = _measure_cuda(
            lambda: model(
                input_ids=device_input_ids,
                attention_mask=attention_mask,
                use_cache=True,
            )
        )
        # gpu_ttft_ms 在此仅计预填充前向，不包含 H2D 或这次首 token 选择，区别于 Week 1。
        next_token = _select_prompt_tokens(prefill_output.logits, attention_mask)
        generated = next_token
        past_key_values = prefill_output.past_key_values
        decode_step_ms: list[float] = []

        for _ in range(output_tokens - 1):

            def decode_step():
                # 每行只输入上一步 token；掩码保留历史 padding 信息，并增加一个有效位置。
                next_attention_mask = torch.cat(
                    [
                        attention_mask,
                        torch.ones(
                            (batch_size, 1),
                            dtype=attention_mask.dtype,
                            device=attention_mask.device,
                        ),
                    ],
                    dim=1,
                )
                output = model(
                    input_ids=next_token,
                    attention_mask=next_attention_mask,
                    past_key_values=past_key_values,
                    use_cache=True,
                )
                token = output.logits[:, -1, :].argmax(dim=-1, keepdim=True)
                return output.past_key_values, next_attention_mask, token

            (past_key_values, attention_mask, next_token), elapsed_ms = _measure_cuda(
                decode_step
            )
            # KV 和掩码已更新；拼接输出用于记录，不在本步 _measure_cuda 的计时范围内。
            generated = torch.cat([generated, next_token], dim=1)
            decode_step_ms.append(elapsed_ms)

        # 先读取张量分配/分配器保留显存峰值，再把输出搬回 CPU；不是整张 GPU 的占用。
        peak = current_peak_memory()
        generated_lists = generated.detach().cpu().tolist()

    # 汇总各测量阶段；预处理单独传入，吞吐率按整个 batch 的输出 token 数计算。
    latency = summarize_batch_latency(
        preprocessing_ms=preprocessing_ms,
        h2d_ms=h2d_ms,
        gpu_ttft_ms=gpu_ttft_ms,
        decode_step_ms=decode_step_ms,
        batch_size=batch_size,
        output_tokens=output_tokens,
    )
    token_hash = generated_tokens_sha256(generated_lists)
    result = BatchRunResult(
        batch_size=batch_size,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        use_cache=True,
        actual_output_tokens=int(latency["actual_output_tokens"]),
        preprocessing_ms=float(latency["preprocessing_ms"]),
        h2d_ms=float(latency["h2d_ms"]),
        gpu_ttft_ms=float(latency["gpu_ttft_ms"]),
        e2e_ttft_ms=float(latency["e2e_ttft_ms"]),
        mean_tpot_ms=(
            None if latency["mean_tpot_ms"] is None else float(latency["mean_tpot_ms"])
        ),
        p95_itl_ms=(
            None if latency["p95_itl_ms"] is None else float(latency["p95_itl_ms"])
        ),
        generation_ms=float(latency["generation_ms"]),
        e2e_latency_ms=float(latency["e2e_latency_ms"]),
        output_tokens_per_second=float(latency["output_tokens_per_second"]),
        requests_per_second=float(latency["requests_per_second"]),
        output_token_hash=token_hash,
        **memory_fields(baseline, peak, theoretical_kv_cache_bytes),
    )
    return result, generated_lists
