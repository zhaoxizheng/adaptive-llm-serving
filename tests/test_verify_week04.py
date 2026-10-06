from __future__ import annotations

import pytest

from scripts.verify_week04 import _verify_report


RUN_ID = "12345678-1234-4234-8234-123456789abc"


def report_text() -> str:
    return f"""# Week 4: measured

## Environment and Version Contract
Run ID: {RUN_ID}

## Method
Exact trace replay and raw evidence.

## Results
![throughput](results/week04/figures/concurrency-vs-throughput.png)
![ttft](results/week04/figures/concurrency-vs-p99-ttft.png)
![queue](results/week04/figures/request-rate-vs-queue-time.png)
![comparison](results/week04/figures/week03-vs-vllm-balanced.png)

## Week 3 Controlled Comparison
Same trace IDs and requested shapes.

## Operating Point
Measured offered request rate: 4 requests/s.
Measured request throughput: 3.8 requests/s.
Measured output token throughput: 243 tokens/s.
Measured P99 TTFT: 190 ms.
Measured P99 TPOT: 9 ms.
Measured queue time: 4 ms.
Measured requested count: 200 requests.
Measured success count: 200 requests.
Measured timeout count: 0 requests.
Measured error count: 0 requests.
Measured error rate: 0%.
Server command: vllm serve model --max-num-seqs 16.

## Limitations
One GPU.

## Resource Cleanup
Externally audited after stop.
"""


def metadata() -> dict[str, object]:
    return {"run_id": RUN_ID}


def analysis() -> dict[str, object]:
    return {
        "selected_operating_point": {
            "request_rate_rps": 4.0,
            "request_throughput_rps": 3.8,
            "output_token_throughput_tps": 243.0,
            "p99_ttft_ms": 190.0,
            "p99_tpot_ms": 9.0,
            "mean_queue_ms": 4.0,
            "requested": 200,
            "success": 200,
            "timeout": 0,
            "error": 0,
            "error_rate": 0.0,
        }
    }


def test_report_gate_accepts_full_measured_operating_point(tmp_path) -> None:
    report = tmp_path / "week04.md"
    report.write_text(report_text(), encoding="utf-8")

    _verify_report(report, metadata(), analysis())


def test_report_gate_rejects_configured_slo_without_measured_dimensions(
    tmp_path,
) -> None:
    report = tmp_path / "week04.md"
    text = report_text().replace(
        "Measured offered request rate: 4 requests/s.\n",
        "Configured SLO: P99 TTFT <= 2000 ms.\n",
    )
    report.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="request rate"):
        _verify_report(report, metadata(), analysis())


def test_report_gate_binds_run_id_and_figures(tmp_path) -> None:
    report = tmp_path / "week04.md"
    report.write_text(
        report_text().replace(RUN_ID, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="run UUID"):
        _verify_report(report, metadata(), analysis())

    report.write_text(
        report_text().replace("week03-vs-vllm-balanced.png", "missing.png"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="all figures"):
        _verify_report(report, metadata(), analysis())
