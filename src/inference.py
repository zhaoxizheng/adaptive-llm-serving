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


def token_sequence_hash(tokens: list[int]) -> str:
    return hashlib.sha256(bytes(str(tokens), "utf-8")).hexdigest()[:16]


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
    # 将模型及其子模块切换到评估模式：关闭 Dropout；若有 BatchNorm，则使用已记录的统计量。
    # eval() 不关闭梯度记录；生成函数上的 @torch.inference_mode() 负责关闭梯度记录等开销。
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
    """构造固定 token 长度的测试输入，返回输入张量和构造耗时（毫秒）。"""
    started = time.perf_counter()
    # 将提示词编码为包含特殊 token 的张量，取出单条输入的一维 token 序列。
    encoded = tokenizer(prompt, add_special_tokens=True, return_tensors="pt").input_ids[0]
    # 空序列无法重复构造目标输入，也会导致后续计算重复次数时除以零。
    if encoded.numel() == 0:
        raise ValueError("Prompt tokenization produced no tokens.")
    # 向上取整，确保重复后的 token 数足以覆盖目标长度；不使用 padding。
    repeats = math.ceil(target_tokens / encoded.numel())
    # 重复后截取指定长度，再增加 batch 维度，得到形状 [1, target_tokens]。
    # 这种构造用于控制性能测试的输入长度，不保证重复后的文本语义自然。
    token_ids = encoded.repeat(repeats)[:target_tokens].unsqueeze(0)
    # 计时包含分词、重复、截断和增加维度，不包含传输到 GPU 或模型推理。
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

    # lambda 将输入和掩码搬到模型所在 GPU，并返回这两个张量。
    # _measure_cuda 返回（操作结果, 耗时毫秒）；嵌套解包得到两个 GPU 张量及 H2D 耗时。
    # H2D 表示主机到设备的传输；计时前后同步 GPU，确保测到任务完成的耗时。
    (device_input, attention_mask), h2d_ms = _measure_cuda(
        lambda: (input_ids.to(model.device), host_attention_mask.to(model.device))
    )
    # 预填充（prefill）：对完整提示词执行一次前向计算，返回各位置的 logits。
    # 开启 use_cache 时还会生成历史 KV 缓存，供后续逐 token 解码复用。
    # prefill_output 是模型输出；prefill_forward_ms 是本次前向耗时，不含 H2D 和首 token 选择。
    prefill_output, prefill_forward_ms = _measure_cuda(
        lambda: model(
            input_ids=device_input,
            attention_mask=attention_mask,
            use_cache=use_cache,
        )
    )

    def select_first_token() -> tuple[torch.Tensor, int]:
        """从预填充结果中贪心选择首个输出 token，返回 GPU 张量和整数 ID。"""
        # logits 的形状为 [batch, 输入长度, 词表大小]。
        # [:, -1, :] 取每条输入最后一个位置的分数，用来预测紧接提示词的 token。
        # 沿词表维度取 argmax，无需 softmax 或随机采样；keepdim 保留维度。
        # 本函数处理单条输入，因此 token 的形状为 [1, 1]，可直接用于下一步解码。
        token = prefill_output.logits[:, -1, :].argmax(dim=-1, keepdim=True)
        # 保留 GPU 张量用于推理，同时用 item() 提取单个值并转为 Python 整数以记录输出。
        return token, int(token.item())

    # 单独测量首 token 选择耗时，包含 argmax 和 item()，不包含之前的预填充。
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
                # 本步只输入 next_token，但注意力掩码需覆盖历史 KV 加上本步输入的完整上下文。
                # 形状为 [1, 提示词长度 + 已生成 token 数]；已生成序列包含本步输入的 next_token。
                # 没有 padding，因此全部填 1，表示所有位置有效；因果限制由模型内部处理。
                # 使用原掩码的数据类型，并直接在模型所在 GPU 上创建，避免从 CPU 搬运。
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
                # 仅首次解码时初始化：拼接提示词和预填充后选出的首 token。
                # 后续轮次直接使用下方已追加新 token 的完整序列，不再执行此初始化。
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
                # 每轮将刚预测出的 token 追加到完整序列，供下一轮重新计算全部上下文。
                # 这不是重复初始化：上方首次拼接的是已有首 token，这里追加的是本轮新 token。
                # 例如本轮输入 [提示词, A] 预测出 B，下一轮输入就变为 [提示词, A, B]。
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
    # 为生成的 token 序列计算摘要，便于比较不同缓存模式的输出是否一致。
    token_hash = token_sequence_hash(generated)
    # 将实验参数、分阶段耗时、吞吐率和显存指标封装为结构化结果。
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
        # 统计 PyTorch 张量分配的峰值显存，按 1024² 字节换算；不是整张 GPU 的占用。
        peak_memory_mb=torch.cuda.max_memory_allocated() / (1024**2),
        output_token_hash=token_hash,
    )
    # 返回性能指标及新生成的 token ID；不包含提示词 token，也不在此解码为文本。
    return result, generated
