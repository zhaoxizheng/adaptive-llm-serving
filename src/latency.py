from __future__ import annotations

import math


def summarize_latency(
    *,
    tokenization_ms: float,
    h2d_ms: float,
    prefill_forward_ms: float,
    first_token_selection_ms: float,
    decode_step_ms: list[float],
    output_tokens: int,
) -> dict[str, float | None]:
    """汇总单次生成的分阶段耗时，所有输入和延迟输出均以毫秒为单位。

    TTFT 表示首 token 延迟；TPOT 表示首 token 之后每个输出 token 的生成延迟。
    端到端指标额外包含输入构造耗时，不包含模型加载、文本解码或结果写盘。
    仅生成一个 token 时没有后续解码步骤，因此所有 TPOT 指标返回 None。
    """
    if output_tokens < 1:
        raise ValueError("output_tokens must be at least 1")
    # 首 token 已由预填充和首 token 选择覆盖，其余每个 token 应对应一次解码测量。
    if len(decode_step_ms) != output_tokens - 1:
        raise ValueError(
            "decode_step_ms must contain exactly output_tokens - 1 measurements"
        )
    # 各阶段耗时必须是有限的非负数，排除 NaN、无穷大和负值。
    for name, value in {
        "tokenization_ms": tokenization_ms,
        "h2d_ms": h2d_ms,
        "prefill_forward_ms": prefill_forward_ms,
        "first_token_selection_ms": first_token_selection_ms,
    }.items():
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and non-negative")
    if any(not math.isfinite(value) or value < 0 for value in decode_step_ms):
        raise ValueError("decode step measurements must be finite and non-negative")

    # 本项目的推理 TTFT 包含主机到 GPU 的传输、完整提示词前向和首 token 选择。
    inference_ttft_ms = h2d_ms + prefill_forward_ms + first_token_selection_ms
    # 解码总耗时仅统计首 token 之后的 output_tokens - 1 个生成步骤。
    decode_ms = sum(decode_step_ms)
    # 总生成耗时为各测量阶段之和，不是对整个函数另设计时器得到的墙钟时间。
    total_generation_ms = inference_ttft_ms + decode_ms
    # 吞吐率以总生成耗时为分母，因此要求该值严格为正。
    if total_generation_ms <= 0:
        raise ValueError("total generation time must be positive")
    # 排序用于计算 nearest-rank 分位数：取第 ceil(p * N) 个样本，不做插值。
    ordered_steps = sorted(decode_step_ms)
    return {
        "inference_ttft_ms": inference_ttft_ms,
        # 端到端首 token 延迟在推理 TTFT 基础上加上输入构造耗时。
        "end_to_end_ttft_ms": tokenization_ms + inference_ttft_ms,
        "decode_ms": decode_ms,
        # 平均 TPOT 只对后续解码步骤求均值，不将首 token 延迟摊入。
        "mean_tpot_ms": (
            sum(decode_step_ms) / len(decode_step_ms) if decode_step_ms else None
        ),
        # P50、P95 表示后续单步解码延迟的 50% 和 95% 分位数。
        "p50_tpot_ms": (
            ordered_steps[max(math.ceil(0.50 * len(ordered_steps)) - 1, 0)]
            if ordered_steps
            else None
        ),
        "p95_tpot_ms": (
            ordered_steps[max(math.ceil(0.95 * len(ordered_steps)) - 1, 0)]
            if ordered_steps
            else None
        ),
        "total_generation_ms": total_generation_ms,
        # 端到端总耗时额外计入输入构造；吞吐率则不计入这一部分。
        "end_to_end_ms": tokenization_ms + total_generation_ms,
        # 毫秒转为秒；分子包含首 token，分母包含首 token 阶段及全部后续解码。
        "output_tokens_per_second": output_tokens / (total_generation_ms / 1_000),
    }
