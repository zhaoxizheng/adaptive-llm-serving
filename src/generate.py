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
    """解析单次生成的配置、输入输出长度、缓存开关和结果路径。"""
    parser = argparse.ArgumentParser(description="Run one measured greedy generation.")
    parser.add_argument("--config", default="configs/week01.yaml")
    parser.add_argument("--prompt-tokens", type=int, default=32)
    parser.add_argument("--output-tokens", type=int, default=32)
    # 默认启用 KV Cache；传入此参数后关闭，便于比较缓存对推理的影响。
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--output")
    return parser.parse_args()


def main() -> None:
    """执行一次带性能测量的贪心生成，并保存冒烟测试证据。"""
    args = parse_args()
    config = load_yaml(args.config)
    # 记录源码身份，并要求关键实验代码和配置已提交，避免结果无法追溯。
    source = source_identity()
    require_clean_source(source)
    # 读取准备阶段的模型清单，校验模型版本、快照指纹及本地文件完整性。
    snapshot = read_json(config["output"]["model_snapshot"])
    validate_model_snapshot(config, snapshot)
    # 收集运行环境并固定随机种子，供结果追溯和实验复现使用。
    runtime = collect_runtime_identity(config)
    torch.manual_seed(config["generation"]["seed"])
    # 从已校验的本地快照加载分词器和模型；加载函数要求 CUDA GPU。
    tokenizer, model, dtype = load_model(
        config["model"]["id"],
        config["model"]["revision"],
        config["model"]["dtype"],
        local_files_only=bool(config["model"].get("local_files_only", True)),
        model_path=str(snapshot["snapshot_path"]),
    )
    # 将提示词分词后重复、截断到指定 token 数，并记录这段输入构造耗时。
    input_ids, tokenization_ms = build_exact_length_input(
        tokenizer,
        config["generation"]["prompt"],
        args.prompt_tokens,
    )
    # 每步选择概率最高的 token，生成指定数量；测量传输、首 token、解码和显存等指标。
    # 将输入构造耗时传入，供生成函数计算端到端指标；模型加载不在本次生成计时内。
    result, generated = run_greedy_generation(
        model,
        input_ids,
        args.output_tokens,
        use_cache=not args.no_cache,
        tokenization_ms=tokenization_ms,
    )
    # 仅解码新生成的 token，跳过特殊标记；保留原始 token ID 以便核对输出。
    generated_text = tokenizer.decode(generated, skip_special_tokens=True)
    # 汇总运行标识、源码与环境指纹、模型信息、性能指标和生成内容。
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
    # 命令行输出路径优先；未指定时使用配置中的冒烟测试结果路径。
    output = Path(args.output or config["output"]["smoke_json"])
    write_json(output, payload)
    print(json.dumps(payload, indent=2))
    print(f"\nSmoke evidence written to {output}")


if __name__ == "__main__":
    main()

