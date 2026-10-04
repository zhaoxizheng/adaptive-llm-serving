from __future__ import annotations

import csv
import io
import json
from copy import deepcopy
from hashlib import sha256

import pytest

from src.analyze_week03 import (
    FIGURE_NAMES,
    SUMMARY_FIELDS,
    analysis_payload,
    analyze,
    summarize_case,
    summarize_results,
    summary_csv_text,
    write_figures,
)
from src.common import load_yaml, write_json
from src.serve_week03 import FakeBatchBackend, simulate_case
from src.week03_contract import BATCH_FIELDS, EVENT_FIELDS, CaseKey, create_run_metadata
from src.workload import TraceRequest


def evidence(
    policy: str,
    load: str,
    delay: int,
    size: int,
    repeat: int = 0,
    metadata=None,
):
    config = load_yaml("configs/week03.yaml")
    case = CaseKey("primary", load, repeat, policy, size, delay)
    metadata = metadata or create_run_metadata(
        config, profile="primary", backend="fake"
    )
    trace = tuple(
        TraceRequest(index, f"r{index}", index * 5_000_000, 256, 64, measurement=True)
        for index in range(30)
    )
    events, batches = simulate_case(
        trace=trace,
        case=case,
        config=config,
        metadata=metadata,
        backend=FakeBatchBackend({1: 20, 4: 40, 8: 60}),
        arrival_rate_rps=25 * float(load),
    )
    return events, batches, metadata


def test_summary_counts_all_terminal_statuses_and_latency() -> None:
    events, batches, metadata = evidence("size_or_time", "0.75", 10, 8)
    summary = summarize_case(events, batches, metadata=metadata)

    assert summary["run_id"] == metadata["run_id"]
    assert summary["metadata_fingerprint"] == metadata["metadata_fingerprint"]
    assert summary["config_fingerprint"] == metadata["config_fingerprint"]
    assert summary["calibration_id"] == metadata["calibration_id"]
    assert summary["case_id"] == events[0]["case_id"]
    assert summary["trace_id"] == events[0]["trace_id"]
    assert summary["attempted"] == 30
    assert summary["completed"] == 30
    assert summary["p99_ttft_ms"] >= summary["p50_ttft_ms"]
    assert 0 < summary["mean_batch_fill_ratio"] <= 1
    assert summary["measurement_duration_seconds"] == 100
    assert summary["achieved_throughput_rps"] == pytest.approx(0.3)


def test_throughput_uses_declared_window_and_reports_drain_separately() -> None:
    events, batches, metadata = evidence("no_batching", "0.75", 0, 1)
    delayed = deepcopy(events)
    # Model a backlog draining 10 seconds after the arrival horizon without
    # changing how many measurement-window requests completed.
    last = delayed[-1]
    extra = 200_000_000_000
    for field in ("first_token_ns", "finished_ns", "terminal_ns"):
        last[field] = int(str(last[field])) + extra
    last["service_time_ns"] = int(str(last["finished_ns"])) - int(
        str(last["gpu_start_ns"])
    )
    last["ttft_ns"] = int(str(last["first_token_ns"])) - int(
        str(last["admitted_ns"])
    )
    last["e2e_latency_ns"] = int(str(last["finished_ns"])) - int(
        str(last["admitted_ns"])
    )
    last_batch = batches[-1]
    last_batch["first_token_ns"] = last["first_token_ns"]
    last_batch["finished_ns"] = last["finished_ns"]
    last_batch["service_time_ns"] = last["service_time_ns"]

    summary = summarize_case(delayed, batches, metadata=metadata)

    assert summary["achieved_throughput_rps"] == pytest.approx(30 / 100)
    assert summary["drain_inclusive_throughput_rps"] < (30 / 100)


def test_generates_the_four_named_plots(tmp_path) -> None:
    all_events = []
    summaries = []
    policies = (
        ("no_batching", 1, 0),
        ("fixed_window", 8, 10),
        ("size_or_time", 8, 10),
    )
    for policy, size, delay in policies:
        for load in ("0.25", "0.75", "1.05"):
            events, batches, _ = evidence(policy, load, delay, size)
            all_events.extend(events)
            summaries.append(summarize_case(events, batches, metadata=_))
    # Add the second batch size needed by the window plot.
    for policy in ("fixed_window", "size_or_time"):
        events, batches, _ = evidence(policy, "0.75", 2, 4, repeat=1)
        all_events.extend(events)
        summaries.append(summarize_case(events, batches, metadata=_))

    outputs = write_figures(summaries, all_events, tmp_path)

    assert tuple(path.name for path in outputs) == FIGURE_NAMES
    assert all(path.is_file() and path.stat().st_size > 0 for path in outputs)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("run_id", "00000000-0000-0000-0000-000000000000"),
        ("metadata_fingerprint", "0" * 64),
        ("config_fingerprint", "different-config"),
        ("calibration_id", "different-calibration"),
        ("case_id", "different-case"),
        ("trace_id", "different-trace"),
        ("arrival_rate_rps", "999.0"),
        ("delay_ms", "11"),
    ),
)
def test_summary_rejects_mixed_identity_or_dimensions(
    field: str, replacement: str
) -> None:
    events, batches, _ = evidence("size_or_time", "0.75", 10, 8)
    tampered = deepcopy(events)
    tampered[1][field] = replacement

    with pytest.raises(ValueError, match=f"mixed {field}"):
        summarize_case(tampered, batches, measurement_duration_seconds=100)


def test_summarize_results_groups_same_case_id_by_run() -> None:
    first_events, first_batches, first_metadata = evidence("no_batching", "0.75", 0, 1)
    second_events, second_batches, second_metadata = evidence(
        "no_batching", "0.75", 0, 1
    )

    summaries = summarize_results(
        [*second_events, *first_events],
        [*second_batches, *first_batches],
        measurement_duration_seconds=100,
    )

    assert len(summaries) == 2
    assert {row["run_id"] for row in summaries} == {
        first_metadata["run_id"],
        second_metadata["run_id"],
    }
    assert {row["case_id"] for row in summaries} == {first_events[0]["case_id"]}


def test_deterministic_summary_csv_and_analysis_payload() -> None:
    first_events, first_batches, first_metadata = evidence("no_batching", "0.75", 0, 1)
    second_events, second_batches, _ = evidence(
        "size_or_time", "0.75", 10, 8, metadata=first_metadata
    )
    summaries = [
        summarize_case(second_events, second_batches, metadata=first_metadata),
        summarize_case(first_events, first_batches, metadata=first_metadata),
    ]

    serialized = summary_csv_text(summaries)
    assert serialized == summary_csv_text(list(reversed(summaries)))
    assert serialized.splitlines()[0] == ",".join(SUMMARY_FIELDS)
    assert "\r" not in serialized
    parsed = list(csv.DictReader(io.StringIO(serialized)))
    assert [row["case_id"] for row in parsed] == sorted(
        row["case_id"] for row in summaries
    )

    arguments = {
        "metadata": first_metadata,
        "events_sha256": "a" * 64,
        "batches_sha256": "b" * 64,
        "figures": [f"figures/{name}" for name in FIGURE_NAMES],
    }
    first_payload = analysis_payload(summaries, **arguments)
    second_payload = analysis_payload(list(reversed(summaries)), **arguments)
    first_bytes = (json.dumps(first_payload, indent=2, sort_keys=True) + "\n").encode()
    second_bytes = (
        json.dumps(second_payload, indent=2, sort_keys=True) + "\n"
    ).encode()

    assert first_bytes == second_bytes
    assert first_payload["run_id"] == first_metadata["run_id"]
    assert (
        first_payload["metadata_fingerprint"] == first_metadata["metadata_fingerprint"]
    )
    assert first_payload["config_fingerprint"] == first_metadata["config_fingerprint"]
    assert first_payload["calibration_id"] == first_metadata["calibration_id"]
    assert first_payload["profile"] == "primary"
    assert first_payload["backend"] == "fake"
    assert first_payload["input_sha256"] == {
        "events.csv": "a" * 64,
        "batches.csv": "b" * 64,
    }
    assert first_payload["case_count"] == 2


def test_no_completed_requests_have_blank_latency_evidence() -> None:
    config = load_yaml("configs/week03.yaml")
    case = CaseKey("smoke", "0.75", 0, "no_batching", 1, 0)
    metadata = create_run_metadata(config, profile="smoke", backend="fake")
    events, batches = simulate_case(
        trace=(TraceRequest(0, "r0", 0, 256, 64, measurement=True),),
        case=case,
        config=config,
        metadata=metadata,
        backend=FakeBatchBackend({1: 20}, fail_batch_ids=("batch-000000",)),
        arrival_rate_rps=18.75,
    )

    summary = summarize_case(events, batches, metadata=metadata)
    serialized = next(csv.DictReader(io.StringIO(summary_csv_text([summary]))))

    assert summary["completed"] == 0
    assert summary["achieved_throughput_rps"] == 0
    assert summary["p50_ttft_ms"] is None
    assert summary["p99_e2e_ms"] is None
    assert summary["p95_queue_delay_ms"] is None
    assert serialized["p50_ttft_ms"] == ""
    assert serialized["p99_e2e_ms"] == ""


def test_figures_reject_all_null_ttft_series(tmp_path) -> None:
    events, batches, _ = evidence("no_batching", "0.75", 0, 1)
    summary = summarize_case(events, batches, metadata=_)
    for field in ("p50_ttft_ms", "p95_ttft_ms", "p99_ttft_ms"):
        summary[field] = None

    with pytest.raises(ValueError, match="no completed TTFT samples"):
        write_figures([summary], events, tmp_path)


def _write_csv(path, rows, fields) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _hf_metadata(config):
    return create_run_metadata(
        config,
        profile="primary",
        backend="hf",
        source={
            "git_commit": "a" * 40,
            "git_dirty": False,
            "critical_source_dirty": False,
            "dirty_state_fingerprint": "b" * 64,
            "source_tree_fingerprint": "c" * 64,
        },
        runtime={"backend": "hf-test"},
        calibration={
            "calibration_id": "d" * 64,
            "calibration_fingerprint": "d" * 64,
            "method": "batch1_median_completed_requests_per_second",
            "capacity_rps": 25.0,
        },
    )


def test_analyze_uses_artifact_root_and_writes_reproducible_outputs(
    tmp_path, monkeypatch
) -> None:
    events, batches, metadata = evidence("no_batching", "0.75", 0, 1)
    root = tmp_path / "simulation"
    events_path = root / "raw" / "events.csv"
    batches_path = root / "raw" / "batches.csv"
    _write_csv(events_path, events, EVENT_FIELDS)
    _write_csv(batches_path, batches, BATCH_FIELDS)
    write_json(root / "raw" / "run_metadata.json", metadata)

    def fake_figures(summaries, raw_events, output_dir):
        del summaries, raw_events
        return tuple(output_dir / name for name in FIGURE_NAMES)

    monkeypatch.setattr("src.analyze_week03.write_figures", fake_figures)
    config = load_yaml("configs/week03.yaml")
    analyze(config, artifact_root=root)
    first_summary = (root / "summary.csv").read_bytes()
    first_analysis = (root / "analysis.json").read_bytes()
    analyze(config, artifact_root=root)

    assert (root / "summary.csv").read_bytes() == first_summary
    assert (root / "analysis.json").read_bytes() == first_analysis
    payload = json.loads(first_analysis)
    assert payload["input_sha256"] == {
        "events.csv": sha256(events_path.read_bytes()).hexdigest(),
        "batches.csv": sha256(batches_path.read_bytes()).hexdigest(),
    }


def test_analyze_rejects_failed_hf_evidence(tmp_path) -> None:
    config = load_yaml("configs/week03.yaml")
    metadata = _hf_metadata(config)
    case = CaseKey("primary", "0.75", 0, "no_batching", 1, 0)
    events, batches = simulate_case(
        trace=(TraceRequest(0, "r0", 0, 256, 64, measurement=True),),
        case=case,
        config=config,
        metadata=metadata,
        backend=FakeBatchBackend({1: 20}, fail_batch_ids=("batch-000000",)),
        arrival_rate_rps=18.75,
    )
    root = tmp_path / "official"
    _write_csv(root / "raw" / "events.csv", events, EVENT_FIELDS)
    _write_csv(root / "raw" / "batches.csv", batches, BATCH_FIELDS)
    write_json(root / "raw" / "run_metadata.json", metadata)

    with pytest.raises(ValueError, match="HF evidence contains failed"):
        analyze(config, artifact_root=root)
