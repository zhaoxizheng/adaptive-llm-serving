from __future__ import annotations

from copy import deepcopy

import pytest

from src.analyze_week04 import (
    FIGURE_NAMES,
    build_analysis_summary,
    generate_week04_plots,
    validate_week03_week04_comparison,
)
from src.common import load_yaml


def record(week: int, *, workload: str = "balanced") -> dict[str, object]:
    return {
        "schema_version": 1,
        "identity": {
            "model": "Qwen/Qwen2.5-0.5B-Instruct",
            "model_revision": "a" * 40,
            "dtype": "bfloat16",
        },
        "case": {
            "workload": workload,
            "requested_prompt_tokens": 256,
            "requested_output_tokens": 64,
            "request_rate": 4.0,
            "concurrency": None,
            "repeat": 0,
            "trace_id": "trace-balanced-rate4-repeat0",
        },
        "counts": {"requested": 10, "success": 10, "timeout": 0, "error": 0},
        "tokens": {},
        "metrics": {
            "request_throughput": 3.5 if week == 3 else 3.8,
            "output_token_throughput": 224.0,
            "p99_ttft_ms": 30.0 if week == 3 else 20.0,
            "p99_tpot_ms": 4.0,
            "server_queue_ms": 5.0 if week == 4 else None,
        },
        "source": {},
        "execution": {
            "server_instance_id": "12345678-1234-4234-8234-123456789abc",
            "server_attempt_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        },
    }


def test_balanced_comparison_accepts_matching_identity_and_shape() -> None:
    left, right = validate_week03_week04_comparison([record(3)], [record(4)])
    assert len(left) == len(right) == 1


def test_comparison_excludes_closed_loop_rows_instead_of_using_concurrency_as_rate() -> (
    None
):
    closed = deepcopy(record(4))
    closed["case"]["request_rate"] = None
    closed["case"]["concurrency"] = 4
    closed["case"]["trace_id"] = None

    left, right = validate_week03_week04_comparison([record(3)], [closed, record(4)])

    assert len(left) == len(right) == 1
    assert right[0]["case"]["concurrency"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trace_id", "different-trace"),
        ("request_rate", 8.0),
    ],
)
def test_comparison_rejects_nonidentical_trace_rate_cells(
    field: str, value: object
) -> None:
    week04 = deepcopy(record(4))
    week04["case"][field] = value

    with pytest.raises(ValueError, match="trace_id/request_rate cells differ"):
        validate_week03_week04_comparison([record(3)], [week04])


@pytest.mark.parametrize(
    ("location", "field", "value", "message"),
    [
        ("identity", "model_revision", "b" * 40, "identity mismatch"),
        ("case", "requested_prompt_tokens", 128, "prompt=256"),
        ("case", "workload", "short-chat", "no balanced"),
    ],
)
def test_comparison_rejects_identity_or_workload_drift(
    location: str, field: str, value: object, message: str
) -> None:
    week04 = deepcopy(record(4))
    week04[location][field] = value
    with pytest.raises(ValueError, match=message):
        validate_week03_week04_comparison([record(3)], [week04])


def test_generates_exact_required_figures_and_keeps_queue_separate(tmp_path) -> None:
    closed = deepcopy(record(4))
    closed["case"]["request_rate"] = None
    closed["case"]["concurrency"] = 4
    outputs = generate_week04_plots([closed, record(4)], [record(3)], tmp_path)

    assert tuple(path.name for path in outputs) == FIGURE_NAMES
    assert all(path.is_file() and path.stat().st_size > 0 for path in outputs)

    no_queue = deepcopy(record(4))
    no_queue["metrics"]["server_queue_ms"] = None
    with pytest.raises(ValueError, match="server_queue_ms"):
        generate_week04_plots(
            [closed, no_queue], [record(3)], tmp_path / "missing-queue"
        )


def test_analysis_summary_selects_highest_slo_passing_open_loop_rate() -> None:
    config = load_yaml("configs/week04.yaml")
    low = record(4)
    low["case"]["case_id"] = "open-low"
    low["case"]["run_id"] = "run-1"
    high = deepcopy(low)
    high["case"]["case_id"] = "open-high"
    high["case"]["request_rate"] = 8.0
    high["metrics"]["p99_ttft_ms"] = 2500.0

    summary = build_analysis_summary([low, high], config)

    selected = summary["selected_operating_point"]
    assert selected["case_id"] == "open-low"
    assert selected["request_rate_rps"] == 4.0
    assert selected["slo_pass"] is True
    assert summary["open_loop_results"][1]["slo_pass"] is False
    assert summary["server_instance_id"] == (
        "12345678-1234-4234-8234-123456789abc"
    )
    assert summary["server_attempt_id"] == (
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
