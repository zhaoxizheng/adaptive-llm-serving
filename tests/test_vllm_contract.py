from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from src.vllm_contract import (
    PINNED_VLLM_VERSION,
    benchmark_argv,
    config_fingerprint,
    expand_cases,
    is_loopback_host,
    scientific_config,
    server_argv,
    validate_config,
    validate_help_support,
)

REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"


def make_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "model": {
            "id": "example/model",
            "revision": REVISION,
            "served_model_name": "example",
            "dtype": "auto",
        },
        "server": {
            "host": "127.0.0.1",
            "port": 8000,
            "max_model_len": 4096,
            "gpu_memory_utilization": 0.85,
            "max_num_seqs": 16,
            "max_num_batched_tokens": 4096,
            "enable_prefix_caching": False,
            "startup_timeout_seconds": 600,
            "readiness_poll_seconds": 2,
            "shutdown_timeout_seconds": 30,
            "log_path": "results/server.log",
            "pid_metadata_path": "results/server.json",
            "serve_help_path": "results/serve-help.txt",
            "models_snapshot_path": "results/models.json",
        },
        "smoke": {
            "prompt": "hello",
            "requested_output_tokens": 4,
            "temperature": 0.0,
            "seed": 7,
            "timeout_seconds": 30,
        },
        "benchmark": {
            "backend": "vllm",
            "endpoint": "/v1/completions",
            "dataset_name": "random",
            "seed": 42,
            "warmup_requests": 2,
            "num_prompts": 20,
            "repeats": 2,
            "timeout_seconds": 60,
            "metric_percentiles": [50, 95, 99],
            "concurrency": [1, 2],
            "request_rates": [0.5, 1],
            "workloads": {
                "short-chat": {"prompt_tokens": 128, "output_tokens": 32},
                "balanced": {"prompt_tokens": 256, "output_tokens": 64},
                "long-context": {"prompt_tokens": 2048, "output_tokens": 32},
                "generation": {"prompt_tokens": 128, "output_tokens": 256},
                "mixed": {
                    "prompt_tokens": [128, 256],
                    "output_tokens": [32, 64],
                },
            },
            "primary": {
                "closed_loop_workloads": ["balanced"],
                "open_loop_workloads": ["balanced"],
            },
            "comparison": {
                "workload": "balanced",
                "base_prompt": "same deterministic comparison prompt",
                "week03_results": "results/week03.jsonl",
                "week03_config": "configs/week03.yaml",
                "arrival_trace_dir": "results/week03/raw/traces",
                "week03_run_metadata": "results/week03/raw/run_metadata.json",
                "week03_events": "results/week03/raw/events.csv",
                "week03_verification_receipt": "results/week03/verification-receipt.json",
                "week03_run_status": "results/week03/run-status.json",
                "profile": "primary",
                "backend": "hf",
                "max_workers": 16,
                "max_p99_arrival_lag_ms": 100,
            },
            "slo": {
                "p99_ttft_ms": 2000,
                "p99_tpot_ms": 100,
                "max_error_rate": 0.0,
            },
        },
        "output": {"raw_dir": "results/week04/raw"},
    }


def test_config_fingerprint_ignores_output_paths() -> None:
    config = make_config()
    relocated = deepcopy(config)
    relocated["output"] = {"raw_dir": "/mnt/evidence"}

    assert config_fingerprint(config) == config_fingerprint(relocated)
    assert "output" not in scientific_config(config)
    assert scientific_config(config)["vllm_version"] == PINNED_VLLM_VERSION


def test_config_fingerprint_changes_with_server_parameter() -> None:
    changed = make_config()
    original = config_fingerprint(changed)
    changed["server"]["max_num_seqs"] = 8
    assert config_fingerprint(changed) != original


@pytest.mark.parametrize("revision", ["main", "a" * 39, "A" * 40])
def test_validate_config_rejects_mutable_revision(revision: str) -> None:
    config = make_config()
    config["model"]["revision"] = revision
    with pytest.raises(ValueError, match="immutable lowercase 40-character"):
        validate_config(config)


def test_expand_cases_is_deterministic_unique_cartesian_product() -> None:
    config = make_config()
    first = expand_cases(config)
    second = expand_cases(config)

    assert first == second
    assert len(first) == 8
    assert len({case.case_id for case in first}) == 8
    assert {case.mode for case in first} == {"closed-loop", "open-loop"}
    assert {case.repeat for case in first} == {0, 1}


def test_server_and_benchmark_argv_pin_scientific_inputs() -> None:
    config = make_config()
    server = server_argv(config)
    case = next(case for case in expand_cases(config) if case.mode == "closed-loop")
    benchmark = benchmark_argv(config, case)

    assert server[:3] == ["vllm", "serve", "example/model"]
    assert server[server.index("--revision") + 1] == REVISION
    assert benchmark[:3] == ["vllm", "bench", "serve"]
    assert benchmark[benchmark.index("--max-concurrency") + 1] in {"1", "2"}
    assert benchmark[benchmark.index("--request-rate") + 1] == "inf"
    assert benchmark[benchmark.index("--temperature") + 1] == "0"


def test_validate_help_support_fails_before_unsupported_flags_run() -> None:
    with pytest.raises(ValueError, match="--new-flag"):
        validate_help_support(["vllm", "serve", "m", "--new-flag", "x"], "--host")


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "[::1]"])
def test_loopback_hosts(host: str) -> None:
    assert is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.1", "example.test"])
def test_non_loopback_hosts(host: str) -> None:
    assert not is_loopback_host(host)
