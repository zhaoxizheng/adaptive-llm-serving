from __future__ import annotations

import argparse
import gc
from pathlib import Path
from typing import Mapping

from src.common import (
    load_yaml,
    read_json,
    require_clean_source,
    source_identity,
    utc_now,
    write_json,
)
from src.hf_batch_backend import (
    CudaMemory,
    capture_model_memory_baseline,
    cleanup_cuda_after_failure,
    current_peak_memory,
    is_cuda_oom,
    load_hf_batch_model,
    memory_fields,
    prepare_batch_tensors,
    run_batched_greedy_generation,
)
from src.kv_cache_estimator import estimate_from_model_config, observed_kv_sequence_length
from src.result_store import append_row, read_rows
from src.week02_contract import (
    RESULT_FIELDS,
    CaseSpec,
    collect_runtime_identity,
    create_run_metadata,
    iter_case_specs,
    validate_canonical_matrix,
    validate_model_snapshot,
    validate_official_completion,
    validate_result_rows,
    validate_run_metadata,
)
from src.week02_smoke import (
    failed_smoke_artifact,
    run_preformal_smoke,
    validate_smoke_artifact,
    validate_smoke_formal_hashes,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Week 2 static-batch sweeps.")
    parser.add_argument("--config", default="configs/week02.yaml")
    return parser.parse_args()


def load_or_create_metadata(
    metadata_path: Path,
    output_path: Path,
    config: Mapping[str, object],
    source: Mapping[str, object],
    runtime: Mapping[str, object],
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


def _identity_row(
    case: CaseSpec,
    metadata: Mapping[str, object],
    source: Mapping[str, object],
    runtime: Mapping[str, object],
) -> dict[str, object]:
    return {
        "timestamp": utc_now(),
        "run_id": metadata["run_id"],
        "git_commit": source["git_commit"],
        "config_fingerprint": metadata["config_fingerprint"],
        "runtime_fingerprint": metadata["runtime_fingerprint"],
        "model": runtime["model"],
        "model_revision": runtime["model_revision"],
        "dtype": runtime["dtype"],
        "sweep": case.sweep,
        "case_name": case.name,
        "repeat": case.repeat,
        "batch_size": case.batch_size,
        "prompt_tokens": case.prompt_tokens,
        "output_tokens": case.output_tokens,
        "use_cache": True,
    }


def _failure_row(
    case: CaseSpec,
    metadata: Mapping[str, object],
    source: Mapping[str, object],
    runtime: Mapping[str, object],
    *,
    phase: str,
    error: BaseException,
    baseline: CudaMemory,
    theoretical_kv_cache_bytes: int,
) -> dict[str, object]:
    try:
        peak = current_peak_memory()
    except Exception:
        peak = baseline
    status = "oom" if is_cuda_oom(error) else "error"
    message = " ".join(str(error).split())[:2_000] or error.__class__.__name__
    return {
        **_identity_row(case, metadata, source, runtime),
        "status": status,
        "error_phase": phase,
        "error_type": error.__class__.__name__,
        "error_message": message,
        "actual_output_tokens": "",
        "preprocessing_ms": "",
        "h2d_ms": "",
        "gpu_ttft_ms": "",
        "e2e_ttft_ms": "",
        "mean_tpot_ms": "",
        "p95_itl_ms": "",
        "generation_ms": "",
        "e2e_latency_ms": "",
        "output_tokens_per_second": "",
        "requests_per_second": "",
        **memory_fields(baseline, peak, theoretical_kv_cache_bytes),
        "output_token_hash": "",
    }


def _theoretical_bytes(model_config: object, case: CaseSpec, dtype: str) -> int:
    estimate = estimate_from_model_config(
        model_config,
        sequence_length=observed_kv_sequence_length(
            case.prompt_tokens, case.output_tokens
        ),
        batch_size=case.batch_size,
        dtype=dtype,
    )
    return estimate.bytes


def _workload_key(case: CaseSpec) -> tuple[str, int, int, int]:
    return case.sweep, case.batch_size, case.prompt_tokens, case.output_tokens


def main() -> None:
    """执行静态 batch 测试矩阵，逐条保存成功或失败记录，并支持断点续跑。"""
    args = parse_args()
    config = load_yaml(args.config)
    # 固定正式测试矩阵并绑定已提交的源码，避免不同配置或代码的结果混用。
    validate_canonical_matrix(config)
    source = source_identity()
    require_clean_source(source)
    output = config["output"]
    assert isinstance(output, Mapping)
    output_path = Path(str(output["raw_csv"]))
    metadata_path = Path(str(output["run_metadata"]))
    smoke_path = Path(str(output["smoke_json"]))
    snapshot_path = Path(str(output["model_snapshot"]))
    if not snapshot_path.is_file():
        raise RuntimeError(
            f"Missing model snapshot evidence {snapshot_path}; prepare the model first."
        )
    snapshot = read_json(snapshot_path)
    validate_model_snapshot(config, snapshot)
    runtime = collect_runtime_identity(config)
    metadata = load_or_create_metadata(
        metadata_path, output_path, config, source, runtime
    )
    # 校验已有记录后恢复终态用例；终态也包含已实际测量的 OOM/error，不只是成功。
    existing_rows = read_rows(output_path, expected_fields=RESULT_FIELDS)
    completed = validate_result_rows(
        existing_rows, metadata, config, require_complete=False
    )
    cases = iter_case_specs(config)
    if len(completed) == len(cases):
        validate_result_rows(existing_rows, metadata, config, require_complete=True)
        if not smoke_path.is_file():
            raise RuntimeError(f"Completed matrix is missing smoke evidence {smoke_path}")
        smoke = read_json(smoke_path)
        validate_smoke_artifact(smoke, config, metadata)
        validate_smoke_formal_hashes(smoke, existing_rows)
        validate_official_completion(existing_rows, config)
        print(f"All {len(cases)} Week 2 terminal cases already exist in {output_path}.")
        return
    if completed:
        print(
            f"Resuming run {metadata['run_id']}: "
            f"{len(completed)}/{len(cases)} terminal cases."
        )

    import torch

    generation = config["generation"]
    model_config = config["model"]
    benchmark = config["benchmark"]
    assert isinstance(generation, Mapping)
    assert isinstance(model_config, Mapping)
    assert isinstance(benchmark, Mapping)
    torch.manual_seed(int(generation["seed"]))
    tokenizer, model, dtype = load_hf_batch_model(
        str(model_config["id"]),
        str(model_config["revision"]),
        str(model_config["dtype"]),
        local_files_only=bool(model_config.get("local_files_only", True)),
        model_path=str(snapshot["snapshot_path"]),
    )
    resolved_dtype = str(dtype).removeprefix("torch.")
    if resolved_dtype != runtime["dtype"]:
        raise RuntimeError(
            f"Loaded dtype {resolved_dtype} differs from runtime {runtime['dtype']}"
        )
    # 模型加载后记录 allocated/reserved 显存基线，后续报告峰值及相对基线增量。
    baseline = capture_model_memory_baseline()
    warmup_runs = int(benchmark["warmup_runs"])
    padding_side = str(generation.get("padding_side", "left"))
    prompt = str(generation["prompt"])

    # 正式测试前验证真实 batch 形状及输出一致性；已有 smoke 也必须重新校验。
    if smoke_path.is_file():
        smoke = read_json(smoke_path)
        validate_smoke_artifact(smoke, config, metadata)
        validate_smoke_formal_hashes(smoke, existing_rows)
        print(f"Validated existing pre-formal smoke evidence in {smoke_path}.")
    else:
        try:
            smoke = run_preformal_smoke(
                tokenizer,
                model,
                config,
                metadata,
                baseline=baseline,
                dtype=resolved_dtype,
            )
        except Exception as error:
            write_json(
                smoke_path,
                failed_smoke_artifact(metadata, phase="pre_formal_smoke", error=error),
            )
            raise RuntimeError(
                f"Week 2 pre-formal smoke/parity failed; evidence saved to {smoke_path}"
            ) from error
        write_json(smoke_path, smoke)
        print(f"Pre-formal smoke/parity passed for {smoke['smoke_batch_sizes']}.")

    # 按工作负载分组未完成的重复测试，同一组只执行一轮配置规定的预热。
    workloads: dict[tuple[str, int, int, int], list[CaseSpec]] = {}
    for case in cases:
        if case.key not in completed:
            workloads.setdefault(_workload_key(case), []).append(case)

    written = 0
    for pending in workloads.values():
        first = pending[0]
        # 理论 KV 大小是估算值，不等同于实测显存增量；预热结果不写入正式记录。
        theoretical = _theoretical_bytes(model.config, first, resolved_dtype)
        warmup_error: BaseException | None = None
        try:
            for _ in range(warmup_runs):
                warm_input, warm_mask, warm_preprocessing = prepare_batch_tensors(
                    tokenizer,
                    prompt,
                    batch_size=first.batch_size,
                    prompt_tokens=first.prompt_tokens,
                    padding_side=padding_side,
                )
                run_batched_greedy_generation(
                    model,
                    warm_input,
                    warm_mask,
                    output_tokens=first.output_tokens,
                    preprocessing_ms=warm_preprocessing,
                    baseline=baseline,
                    theoretical_kv_cache_bytes=theoretical,
                )
                del warm_input, warm_mask
        except Exception as error:
            warmup_error = error

        # 预热 OOM 不能代替正式测量：仍逐个尝试重复用例，建立真实容量边界。
        # 非 OOM 的预热异常直接中止，不凭空生成失败行。
        if warmup_error is not None:
            cleanup_cuda_after_failure()
            gc.collect()
            if not is_cuda_oom(warmup_error):
                raise RuntimeError(
                    f"Unexpected warmup failure for {first.name}; no repeat rows were synthesized"
                ) from warmup_error
            print(
                f"{first.name}: warmup OOM; running every formal attempt individually "
                "to establish a measured capacity boundary"
            )

        for case in pending:
            try:
                host_input, host_mask, preprocessing_ms = prepare_batch_tensors(
                    tokenizer,
                    prompt,
                    batch_size=case.batch_size,
                    prompt_tokens=case.prompt_tokens,
                    padding_side=padding_side,
                )
                result, _ = run_batched_greedy_generation(
                    model,
                    host_input,
                    host_mask,
                    output_tokens=case.output_tokens,
                    preprocessing_ms=preprocessing_ms,
                    baseline=baseline,
                    theoretical_kv_cache_bytes=theoretical,
                )
                row = {
                    **_identity_row(case, metadata, source, runtime),
                    "status": "completed",
                    "error_phase": "",
                    "error_type": "",
                    "error_message": "",
                    **result.to_dict(),
                }
                del host_input, host_mask
            except Exception as error:
                row = _failure_row(
                    case,
                    metadata,
                    source,
                    runtime,
                    phase="measurement",
                    error=error,
                    baseline=baseline,
                    theoretical_kv_cache_bytes=theoretical,
                )
                cleanup_cuda_after_failure()
                gc.collect()
            # 正式测量异常记录为 OOM/error，并清理 CUDA；每个实际尝试逐条持久化。
            append_row(output_path, row, RESULT_FIELDS)
            completed.add(case.key)
            written += 1
            if row["status"] == "completed":
                print(
                    f"{case.name}: generation_ms={float(row['generation_ms']):.2f}, "
                    f"tokens/s={float(row['output_tokens_per_second']):.2f}"
                )
            else:
                print(f"{case.name}: {row['status']} during measurement")

    # 完整矩阵不代表全部成功：还需验证 smoke 对比和正式完成条件。
    rows = read_rows(output_path, expected_fields=RESULT_FIELDS)
    validate_result_rows(rows, metadata, config, require_complete=True)
    validate_smoke_formal_hashes(smoke, rows)
    validate_official_completion(rows, config)
    print(
        f"Wrote {written} new terminal rows to {output_path}; "
        f"matrix complete ({len(rows)}/{len(cases)})."
    )


if __name__ == "__main__":
    main()
