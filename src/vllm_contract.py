from __future__ import annotations

import argparse
import ipaddress
import json
import math
import re
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from src.common import load_yaml, stable_fingerprint

SCHEMA_VERSION = 1
PINNED_VLLM_VERSION = "0.10.2"
PINNED_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
FIXED_WORKLOADS = ("short-chat", "balanced", "long-context", "generation")
SUPPORTED_MODES = ("closed-loop", "open-loop")


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    mode: str
    workload: str
    prompt_tokens: int
    output_tokens: int
    repeat: int
    num_prompts: int
    seed: int
    max_concurrency: int | None = None
    request_rate: float | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _sequence(value: object, name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a sequence")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if result < 1 or result != value:
        raise ValueError(f"{name} must be a positive integer")
    return result


def _positive_float(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite positive number")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite positive number") from error
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return result


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_config(config: Mapping[str, object]) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        raise ValueError("Week 4 config must be a mapping")
    if config.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version must equal {SCHEMA_VERSION}")
    for section in ("model", "server", "smoke", "benchmark", "output"):
        if section not in config:
            raise ValueError(f"Week 4 config is missing section: {section}")

    model = _mapping(config["model"], "model")
    for key in ("id", "revision", "served_model_name", "dtype"):
        if not isinstance(model.get(key), str) or not str(model[key]).strip():
            raise ValueError(f"model.{key} must be a non-empty string")
    if not PINNED_REVISION_PATTERN.fullmatch(str(model["revision"])):
        raise ValueError(
            "model.revision must be an immutable lowercase 40-character commit SHA"
        )

    server = _mapping(config["server"], "server")
    host = str(server.get("host", ""))
    if not host:
        raise ValueError("server.host must be non-empty")
    port = _positive_int(server.get("port"), "server.port")
    if port > 65535:
        raise ValueError("server.port must be <= 65535")
    _positive_int(server.get("max_model_len"), "server.max_model_len")
    utilization = _positive_float(
        server.get("gpu_memory_utilization"), "server.gpu_memory_utilization"
    )
    if utilization > 1:
        raise ValueError("server.gpu_memory_utilization must be <= 1")
    _positive_int(server.get("max_num_seqs"), "server.max_num_seqs")
    _positive_int(server.get("max_num_batched_tokens"), "server.max_num_batched_tokens")
    if not isinstance(server.get("enable_prefix_caching"), bool):
        raise ValueError("server.enable_prefix_caching must be a boolean")
    for key in (
        "startup_timeout_seconds",
        "readiness_poll_seconds",
        "shutdown_timeout_seconds",
    ):
        _positive_float(server.get(key), f"server.{key}")

    smoke = _mapping(config["smoke"], "smoke")
    if not isinstance(smoke.get("prompt"), str) or not smoke["prompt"].strip():
        raise ValueError("smoke.prompt must be a non-empty string")
    _positive_int(smoke.get("requested_output_tokens"), "smoke.requested_output_tokens")
    if float(smoke.get("temperature", -1)) != 0.0:
        raise ValueError("smoke.temperature must be 0.0 for deterministic comparison")
    _positive_float(smoke.get("timeout_seconds"), "smoke.timeout_seconds")

    benchmark = _mapping(config["benchmark"], "benchmark")
    if benchmark.get("dataset_name") != "random":
        raise ValueError("benchmark.dataset_name must be random for fixed token shapes")
    for key in ("warmup_requests", "num_prompts", "repeats", "timeout_seconds"):
        _positive_int(benchmark.get(key), f"benchmark.{key}")
    if int(benchmark["warmup_requests"]) > int(benchmark["num_prompts"]):
        raise ValueError("benchmark.warmup_requests cannot exceed num_prompts")
    for index, value in enumerate(
        _sequence(benchmark.get("concurrency"), "benchmark.concurrency")
    ):
        _positive_int(value, f"benchmark.concurrency[{index}]")
    for index, value in enumerate(
        _sequence(benchmark.get("request_rates"), "benchmark.request_rates")
    ):
        _positive_float(value, f"benchmark.request_rates[{index}]")
    percentiles = [
        _positive_float(value, f"benchmark.metric_percentiles[{index}]")
        for index, value in enumerate(
            _sequence(
                benchmark.get("metric_percentiles"), "benchmark.metric_percentiles"
            )
        )
    ]
    if any(value > 100 for value in percentiles) or 99 not in percentiles:
        raise ValueError("benchmark.metric_percentiles must include 99 and be <= 100")

    workloads = _mapping(benchmark.get("workloads"), "benchmark.workloads")
    for name in FIXED_WORKLOADS:
        workload = _mapping(workloads.get(name), f"benchmark.workloads.{name}")
        _positive_int(workload.get("prompt_tokens"), f"{name}.prompt_tokens")
        _positive_int(workload.get("output_tokens"), f"{name}.output_tokens")
    mixed = _mapping(workloads.get("mixed"), "benchmark.workloads.mixed")
    for key in ("prompt_tokens", "output_tokens"):
        values = _sequence(mixed.get(key), f"benchmark.workloads.mixed.{key}")
        if not values:
            raise ValueError(f"benchmark.workloads.mixed.{key} cannot be empty")
        for index, value in enumerate(values):
            _positive_int(value, f"benchmark.workloads.mixed.{key}[{index}]")

    primary = _mapping(benchmark.get("primary"), "benchmark.primary")
    closed = list(
        _sequence(primary.get("closed_loop_workloads"), "closed_loop_workloads")
    )
    opened = list(_sequence(primary.get("open_loop_workloads"), "open_loop_workloads"))
    if set(closed).difference(FIXED_WORKLOADS):
        raise ValueError(
            "closed_loop_workloads contains an unknown or non-fixed workload"
        )
    if set(opened).difference(FIXED_WORKLOADS):
        raise ValueError(
            "open_loop_workloads contains an unknown or non-fixed workload"
        )
    if len(closed) != len(set(closed)) or len(opened) != len(set(opened)):
        raise ValueError("primary workload lists must not contain duplicates")

    comparison = _mapping(benchmark.get("comparison"), "benchmark.comparison")
    if comparison.get("workload") != "balanced":
        raise ValueError("Week 3/4 comparison must use the balanced workload")
    if (
        not isinstance(comparison.get("base_prompt"), str)
        or not str(comparison["base_prompt"]).strip()
    ):
        raise ValueError("benchmark.comparison.base_prompt must be non-empty")
    for key in (
        "week03_results",
        "week03_config",
        "arrival_trace_dir",
        "week03_run_metadata",
        "week03_events",
        "week03_verification_receipt",
        "week03_run_status",
    ):
        if not isinstance(comparison.get(key), str) or not comparison[key]:
            raise ValueError(f"benchmark.comparison.{key} must be a non-empty path")
    if comparison.get("profile") != "primary":
        raise ValueError("Week 3/4 trace comparison must use the primary profile")
    if comparison.get("backend") != "hf":
        raise ValueError("Week 3/4 trace comparison must use the real hf backend")
    _positive_int(comparison.get("max_workers"), "benchmark.comparison.max_workers")
    _positive_float(
        comparison.get("max_p99_arrival_lag_ms"),
        "benchmark.comparison.max_p99_arrival_lag_ms",
    )
    slo = _mapping(benchmark.get("slo"), "benchmark.slo")
    _positive_float(slo.get("p99_ttft_ms"), "benchmark.slo.p99_ttft_ms")
    _positive_float(slo.get("p99_tpot_ms"), "benchmark.slo.p99_tpot_ms")
    error_rate = float(slo.get("max_error_rate", -1))
    if not math.isfinite(error_rate) or not 0 <= error_rate <= 1:
        raise ValueError("benchmark.slo.max_error_rate must be in [0, 1]")

    output = _mapping(config["output"], "output")
    if not output:
        raise ValueError("output cannot be empty")
    for key, value in output.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value:
            raise ValueError("output keys and paths must be non-empty strings")

    normalized = deepcopy(dict(config))
    # Normalize YAML's integer-like scalar values used by fingerprinting.
    normalized["schema_version"] = SCHEMA_VERSION
    return normalized


def scientific_config(config: Mapping[str, object]) -> dict[str, object]:
    validated = validate_config(config)
    return {
        "schema_version": SCHEMA_VERSION,
        "vllm_version": PINNED_VLLM_VERSION,
        "model": deepcopy(validated["model"]),
        "server": {
            key: deepcopy(validated["server"][key])
            for key in (
                "host",
                "port",
                "max_model_len",
                "gpu_memory_utilization",
                "max_num_seqs",
                "max_num_batched_tokens",
                "enable_prefix_caching",
            )
        },
        "smoke": deepcopy(validated["smoke"]),
        "benchmark": deepcopy(validated["benchmark"]),
    }


def config_fingerprint(config: Mapping[str, object]) -> str:
    return stable_fingerprint(scientific_config(config))


def expand_cases(config: Mapping[str, object]) -> list[BenchmarkCase]:
    validated = validate_config(config)
    benchmark = validated["benchmark"]
    workloads = benchmark["workloads"]
    primary = benchmark["primary"]
    repeats = int(benchmark["repeats"])
    num_prompts = int(benchmark["num_prompts"])
    base_seed = int(benchmark["seed"])
    cases: list[BenchmarkCase] = []
    for workload_name in primary["closed_loop_workloads"]:
        workload = workloads[workload_name]
        for concurrency in benchmark["concurrency"]:
            for repeat in range(repeats):
                identity = {
                    "mode": "closed-loop",
                    "workload": workload_name,
                    "prompt_tokens": int(workload["prompt_tokens"]),
                    "output_tokens": int(workload["output_tokens"]),
                    "max_concurrency": int(concurrency),
                    "repeat": repeat,
                    "seed": base_seed + repeat,
                }
                cases.append(
                    BenchmarkCase(
                        case_id=f"closed-{workload_name}-c{concurrency}-r{repeat}-{stable_fingerprint(identity, 8)}",
                        num_prompts=num_prompts,
                        request_rate=None,
                        **identity,
                    )
                )
    for workload_name in primary["open_loop_workloads"]:
        workload = workloads[workload_name]
        for request_rate in benchmark["request_rates"]:
            for repeat in range(repeats):
                identity = {
                    "mode": "open-loop",
                    "workload": workload_name,
                    "prompt_tokens": int(workload["prompt_tokens"]),
                    "output_tokens": int(workload["output_tokens"]),
                    "request_rate": float(request_rate),
                    "repeat": repeat,
                    "seed": base_seed + repeat,
                }
                cases.append(
                    BenchmarkCase(
                        case_id=f"open-{workload_name}-q{float(request_rate):g}-r{repeat}-{stable_fingerprint(identity, 8)}",
                        num_prompts=num_prompts,
                        max_concurrency=None,
                        **identity,
                    )
                )
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Expanded Week 4 case IDs are not unique")
    return cases


def get_case(config: Mapping[str, object], case_id: str) -> BenchmarkCase:
    matches = [case for case in expand_cases(config) if case.case_id == case_id]
    if len(matches) != 1:
        raise ValueError(f"Unknown Week 4 case_id: {case_id}")
    return matches[0]


def server_argv(config: Mapping[str, object], executable: str = "vllm") -> list[str]:
    validated = validate_config(config)
    model = validated["model"]
    server = validated["server"]
    argv = [
        executable,
        "serve",
        str(model["id"]),
        "--revision",
        str(model["revision"]),
        "--served-model-name",
        str(model["served_model_name"]),
        "--host",
        str(server["host"]),
        "--port",
        str(server["port"]),
        "--dtype",
        str(model["dtype"]),
        "--max-model-len",
        str(server["max_model_len"]),
        "--gpu-memory-utilization",
        str(server["gpu_memory_utilization"]),
        "--max-num-seqs",
        str(server["max_num_seqs"]),
        "--max-num-batched-tokens",
        str(server["max_num_batched_tokens"]),
    ]
    argv.append(
        "--enable-prefix-caching"
        if server["enable_prefix_caching"]
        else "--no-enable-prefix-caching"
    )
    return argv


def benchmark_argv(
    config: Mapping[str, object], case: BenchmarkCase, executable: str = "vllm"
) -> list[str]:
    validated = validate_config(config)
    model = validated["model"]
    server = validated["server"]
    benchmark = validated["benchmark"]
    argv = [
        executable,
        "bench",
        "serve",
        "--backend",
        str(benchmark["backend"]),
        "--base-url",
        f"http://127.0.0.1:{server['port']}",
        "--endpoint",
        str(benchmark["endpoint"]),
        "--model",
        str(model["id"]),
        "--served-model-name",
        str(model["served_model_name"]),
        "--dataset-name",
        str(benchmark["dataset_name"]),
        "--random-input-len",
        str(case.prompt_tokens),
        "--random-output-len",
        str(case.output_tokens),
        "--random-range-ratio",
        "0",
        "--num-prompts",
        str(case.num_prompts),
        "--seed",
        str(case.seed),
        "--request-rate",
        "inf" if case.request_rate is None else f"{case.request_rate:g}",
        "--metric-percentiles",
        ",".join(str(value) for value in benchmark["metric_percentiles"]),
        "--percentile-metrics",
        "ttft,tpot,e2el",
        "--temperature",
        "0",
        "--ignore-eos",
        "--save-result",
        "--save-detailed",
        "--metadata",
        f"vllm_version={PINNED_VLLM_VERSION}",
        f"model_revision={model['revision']}",
        f"dtype={model['dtype']}",
        f"case_id={case.case_id}",
        f"mode={case.mode}",
        f"workload={case.workload}",
        f"requested_prompt_tokens={case.prompt_tokens}",
        f"requested_output_tokens={case.output_tokens}",
        f"repeat={case.repeat}",
    ]
    if case.max_concurrency is not None:
        argv.extend(["--max-concurrency", str(case.max_concurrency)])
    return argv


def required_flags(argv: Sequence[str]) -> set[str]:
    return {token.split("=", 1)[0] for token in argv if token.startswith("--")}


def validate_help_support(argv: Sequence[str], help_text: str) -> None:
    missing = sorted(flag for flag in required_flags(argv) if flag not in help_text)
    if missing:
        raise ValueError(f"Installed vLLM help does not advertise flags: {missing}")


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect the Week 4 experiment contract."
    )
    parser.add_argument(
        "command", choices=("validate", "cases", "benchmark-plan", "server-plan")
    )
    parser.add_argument("--config", default="configs/week04.yaml")
    parser.add_argument("--case-id")
    args = parser.parse_args()
    config = load_yaml(args.config)
    validate_config(config)
    if args.command == "validate":
        print(config_fingerprint(config))
    elif args.command == "cases":
        print(json.dumps([case.to_dict() for case in expand_cases(config)], indent=2))
    elif args.command == "server-plan":
        print(json.dumps(server_argv(config), indent=2))
    else:
        if not args.case_id:
            parser.error("benchmark-plan requires --case-id")
        print(
            json.dumps(benchmark_argv(config, get_case(config, args.case_id)), indent=2)
        )


if __name__ == "__main__":
    _main()
