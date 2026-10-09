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
    """遍历配置中的测试矩阵，比较 KV Cache 开关的性能并校验输出一致性。

    每种输入长度、输出长度和缓存模式先预热，再按配置重复测量。
    结果逐条写入 CSV；重启时校验已有数据并跳过已完成用例。
    """
    args = parse_args()
    config = load_yaml(args.config)
    # 记录源码身份，要求关键实验代码和配置已提交，保证结果可追溯。
    source = source_identity()
    require_clean_source(source)
    output_path = Path(config["output"]["raw_csv"])
    metadata_path = Path(config["output"]["run_metadata"])
    # 必须先准备模型快照清单；随后校验模型版本、指纹及本地文件完整性。
    snapshot_path = Path(config["output"]["model_snapshot"])
    if not snapshot_path.is_file():
        raise RuntimeError(
            f"Missing model snapshot evidence {snapshot_path}; run make prepare-model first."
        )
    snapshot = read_json(snapshot_path)
    validate_model_snapshot(config, snapshot)
    runtime = collect_runtime_identity(config)
    # 新实验创建运行元数据；续跑则要求原元数据与当前配置、源码和环境匹配。
    metadata = load_or_create_metadata(
        metadata_path,
        output_path,
        config,
        source,
        runtime,
    )
    # 校验已有 CSV 并恢复已完成用例；此处允许测试矩阵尚未跑完。
    existing_rows = read_rows(output_path, expected_fields=RESULT_FIELDS)
    completed = validate_result_rows(
        existing_rows,
        metadata,
        config,
        require_complete=False,
    )
    # 用例由输入长度、输出长度、重复编号和缓存模式共同标识。
    expected = expected_cases(config)
    # 已全部完成时直接校验退出，不再加载模型或重新测试。
    if completed == expected:
        validate_result_rows(existing_rows, metadata, config, require_complete=True)
        print(f"All {len(expected)} cases are complete and valid in {output_path}.")
        return
    if completed:
        print(
            f"Resuming run {metadata['run_id']}: "
            f"{len(completed)}/{len(expected)} cases complete."
        )

    # 固定随机种子，从已校验的本地快照加载模型；加载只做一次，不计入生成耗时。
    torch.manual_seed(config["generation"]["seed"])
    tokenizer, model, dtype = load_model(
        config["model"]["id"],
        config["model"]["revision"],
        config["model"]["dtype"],
        local_files_only=bool(config["model"].get("local_files_only", True)),
        model_path=str(snapshot["snapshot_path"]),
    )
    # 实际加载的精度必须与记录的运行环境一致，避免混合不同精度的结果。
    resolved_dtype = str(dtype).removeprefix("torch.")
    if resolved_dtype != runtime["dtype"]:
        raise RuntimeError(
            f"Loaded dtype {resolved_dtype} does not match runtime contract {runtime['dtype']}"
        )
    # 仅比较生成序列的前 parity_tokens 个 token；输出较短时比较全部输出。
    parity_tokens = int(config["benchmark"]["parity_tokens"])
    if parity_tokens < 1:
        raise ValueError("benchmark.parity_tokens must be at least 1")

    # 为每种提示词长度构造可复用的预热输入；正式测量时会重新构造并计时。
    inputs = {
        prompt_tokens: build_exact_length_input(
            tokenizer,
            config["generation"]["prompt"],
            prompt_tokens,
        )
        for prompt_tokens in config["benchmark"]["prompt_tokens"]
    }
    # 覆盖所有输入/输出长度和缓存模式，减少首次执行开销对正式测量的影响。
    # 预热结果不写入 CSV；断点续跑时也会重新执行完整预热。
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

    # 从已有结果恢复输出前缀摘要，便于续跑时继续比较缓存开关的输出。
    # 键不包含缓存模式，使相同输入长度、输出长度、重复编号的模式共享参考值。
    reference_hashes: dict[tuple[int, int, int], str] = {}
    for row in existing_rows:
        key = (int(row["prompt_tokens"]), int(row["output_tokens"]), int(row["repeat"]))
        reference_hashes.setdefault(key, row["parity_token_hash"])

    # 遍历输入长度 × 输出长度 × 重复次数 × 缓存模式的正式测试矩阵。
    written = 0
    for prompt_tokens in config["benchmark"]["prompt_tokens"]:
        for output_tokens in config["benchmark"]["output_tokens"]:
            for repeat in range(config["benchmark"]["repeats"]):
                for use_cache in config["benchmark"]["cache_modes"]:
                    current_case = (prompt_tokens, output_tokens, repeat, use_cache)
                    # 已落盘并通过校验的用例不重复测量，实现断点续跑。
                    if current_case in completed:
                        print(
                            f"skip prompt={prompt_tokens:4d} output={output_tokens:3d} "
                            f"cache={str(use_cache):5s} repeat={repeat}"
                        )
                        continue
                    # 每个正式用例重新构造输入，记录本次输入构造耗时。
                    input_ids, tokenization_ms = build_exact_length_input(
                        tokenizer,
                        config["generation"]["prompt"],
                        prompt_tokens,
                    )
                    # 使用统一的贪心生成函数，测量传输、预填充、解码和峰值显存等指标。
                    result, generated = run_greedy_generation(
                        model,
                        input_ids,
                        output_tokens,
                        use_cache=use_cache,
                        tokenization_ms=tokenization_ms,
                    )
                    # 第一种模式建立参考摘要，后续模式必须生成相同的 token 前缀。
                    # 对比的是 token ID 而非解码文本；不一致时停止，避免比较不同输出的性能。
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
                    # 将性能指标与运行身份、配置/环境指纹及输出摘要合并为一条记录。
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
                    # 每完成一个用例就追加到 CSV，再更新进度，供后续启动恢复。
                    append_row(output_path, row, RESULT_FIELDS)
                    completed.add(current_case)
                    written += 1
                    print(
                        f"prompt={prompt_tokens:4d} output={output_tokens:3d} "
                        f"cache={str(use_cache):5s} repeat={repeat} "
                        f"total_ms={result.total_generation_ms:.2f} "
                        f"tok/s={result.output_tokens_per_second:.2f}"
                    )

    # 重新读取落盘数据，要求完整测试矩阵及各条记录都通过校验。
    rows = read_rows(output_path, expected_fields=RESULT_FIELDS)
    validate_result_rows(rows, metadata, config, require_complete=True)
    print(
        f"Wrote {written} new rows to {output_path}; "
        f"run {metadata['run_id']} is complete ({len(rows)}/{len(expected)} cases)."
    )


if __name__ == "__main__":
    main()
