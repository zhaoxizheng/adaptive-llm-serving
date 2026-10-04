"""CPU-only configuration contract shared by Weeks 5 through 8."""

from __future__ import annotations

import copy
import math
import re
from pathlib import Path

from src.common import load_yaml, stable_fingerprint
from src.vllm_contract import server_argv, validate_config

VLLM_VERSION = "0.10.2"
VLLM_COMMIT = "01efc7ef781391e744ed08c3292817a773d654e6"


def positive(value, name, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    if not math.isfinite(value) or value <= 0 or (integer and int(value) != value):
        raise ValueError(f"{name} must be positive and finite")
    return int(value) if integer else float(value)


def fingerprint(value):
    return stable_fingerprint(value, length=64)


def load_study(path):
    config = load_yaml(path)
    if config.get("schema_version") != 1 or config.get("week") not in (5, 6, 7, 8):
        raise ValueError("expected a Week 5-8 schema_version=1 configuration")
    return config


def validate_week05(config, *, execution=False):
    base = validate_config(load_yaml(config["baseline_config"]))
    load = config["load"]
    for key in ("repeats", "warmup_requests", "max_inflight", "max_requests"):
        positive(load[key], key, integer=True)
    for key in ("duration_seconds", "cooldown_seconds", "request_timeout_seconds"):
        positive(load[key], key)
    if load["repeats"] < 3:
        raise ValueError("capacity needs at least three repeats")
    if not load["multipliers"] or len(set(load["multipliers"])) != len(load["multipliers"]):
        raise ValueError("load multipliers must be nonempty and unique")
    for value in load["multipliers"]:
        positive(value, "load multiplier")
    for name, shape in config["workloads"].items():
        for key in ("prompt_tokens", "output_tokens"):
            positive(shape[key], key, integer=True)
        if shape["output_tokens"] < 2:
            raise ValueError("TPOT workloads require at least two output tokens")
        if sum(shape.values()) > base["server"]["max_model_len"]:
            raise ValueError(f"{name} exceeds max_model_len")
        slo = config["slos"][name]
        if not {"ttft_ms", "tpot_ms"} <= slo.keys():
            raise ValueError(f"{name} needs TTFT and TPOT SLOs")
        for key, value in slo.items():
            if key not in {"ttft_ms", "tpot_ms", "e2e_ms"}:
                raise ValueError(f"unknown SLO metric: {key}")
            positive(value, key)
    for name, weights in config["mixtures"].items():
        if not weights or not weights.keys() <= config["workloads"].keys():
            raise ValueError(f"invalid mixture: {name}")
        for value in weights.values():
            positive(value, "mixture weight")
        if not math.isclose(sum(weights.values()), 1.0):
            raise ValueError(f"mixture weights must sum to one: {name}")
    cap = config["capacity"]
    for key in ("max_error_rate", "max_timeout_rate", "min_throughput_ratio"):
        if not 0 <= cap[key] <= 1:
            raise ValueError(f"invalid {key}")
    for key in ("max_queue_growth_rps", "max_arrival_lag_ms"):
        positive(cap[key], key)
    obs = config["observability"]
    for key in ("step_seconds", "scrape_interval_seconds", "gpu_interval_seconds"):
        positive(obs[key], key)
    if execution:
        calibration = config["calibration"]
        if calibration["confirmed"] is not True:
            raise ValueError("calibrate and freeze the Week 5 SLO/load contract first")
        positive(calibration["baseline_rps"], "calibration.baseline_rps")
        if not calibration.get("evidence") or not Path(calibration["evidence"]).is_file():
            raise ValueError("calibration.evidence must point to saved Week 4 evidence")
    return base


def variant_config(base, variant, tokenizer):
    result = copy.deepcopy(base)
    if not re.fullmatch(r"[0-9a-f]{40}", variant["revision"]):
        raise ValueError("variant revision must be an immutable commit")
    result["model"].update(
        id=variant["model"], revision=variant["revision"], dtype=variant["compute_dtype"]
    )
    validate_config(result)
    return result


def serve_command(base, *, variant=None, tokenizer=None, overrides=None):
    base = copy.deepcopy(base)
    if variant:
        base = variant_config(base, variant, tokenizer)
    overrides = overrides or {}
    allowed = {
        "max_num_seqs",
        "max_num_batched_tokens",
        "gpu_memory_utilization",
        "enable_chunked_prefill",
        "num_gpu_blocks_override",
    }
    if not overrides.keys() <= allowed:
        raise ValueError("unsupported server override")
    for key in allowed & base["server"].keys() & overrides.keys():
        base["server"][key] = overrides[key]
    argv = server_argv(base)
    if tokenizer:
        if not re.fullmatch(r"[0-9a-f]{40}", tokenizer["revision"]):
            raise ValueError("tokenizer revision must be immutable")
        argv += ["--tokenizer", tokenizer["id"], "--tokenizer-revision", tokenizer["revision"]]
    if variant:
        argv += ["--kv-cache-dtype", variant["kv_cache_dtype"]]
        if variant["quantization"]:
            argv += ["--quantization", variant["quantization"]]
    if "enable_chunked_prefill" in overrides:
        argv += [
            (
                "--enable-chunked-prefill"
                if overrides["enable_chunked_prefill"]
                else "--no-enable-chunked-prefill"
            )
        ]
    if "num_gpu_blocks_override" in overrides:
        positive(overrides["num_gpu_blocks_override"], "num_gpu_blocks_override", integer=True)
        argv += ["--num-gpu-blocks-override", str(overrides["num_gpu_blocks_override"])]
    return argv
