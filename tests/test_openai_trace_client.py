from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import src.openai_trace_client as trace_client
from src.openai_trace_client import (
    _read_week03_verification,
    completion_payload,
    deterministic_prompt_token_ids,
    execute_trace,
    plan_trace,
    run_all_configured,
)
from src.week04_contract import (
    create_run_metadata,
    validate_artifact_identity,
)
from src.workload import TraceRequest, trace_fingerprint, write_trace
from src.week03_contract import case_id as week03_case_id, expand_matrix
from tests.test_vllm_contract import make_config
from tests.test_week04_contract import make_runtime, make_source

SERVER_INSTANCE_ID = "12345678-1234-4234-8234-123456789abc"
SERVER_ATTEMPT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class FakeTokenizer:
    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        assert add_special_tokens is True
        return [sum(map(ord, word)) % 251 + 1 for word in text.split()] or [1]


def config(tmp_path):
    value = make_config()
    value["output"].update(
        {
            "trace_replay_dir": str(tmp_path / "replays"),
            "comparison_manifest": str(tmp_path / "comparison-manifest.json"),
            "run_metadata": str(tmp_path / "run-metadata.json"),
        }
    )
    comparison = value["benchmark"]["comparison"]
    week03 = trace_client.load_yaml("configs/week03.yaml")
    week03["workload"].update(duration_seconds=0.003, warmup_seconds=0.001)
    comparison["base_prompt"] = week03["workload"]["prompt"]
    week03_config = tmp_path / "week03.yaml"
    week03_root = tmp_path / "week03"
    week03["output"].update(
        {
            "root": str(week03_root),
            "run_metadata": str(tmp_path / "week03-metadata.json"),
            "raw_events_csv": str(tmp_path / "week03-events.csv"),
            "summary_csv": str(tmp_path / "week03-summary.csv"),
            "verification_receipt": str(tmp_path / "week03-verification-receipt.json"),
            "run_status": str(tmp_path / "week03-run-status.json"),
        }
    )
    week03_config.write_text(yaml.safe_dump(week03, sort_keys=False), encoding="utf-8")
    comparison["week03_config"] = str(week03_config)
    comparison["week03_results"] = str(tmp_path / "week03-summary.csv")
    comparison["week03_run_metadata"] = str(tmp_path / "week03-metadata.json")
    comparison["week03_events"] = str(tmp_path / "week03-events.csv")
    comparison["week03_verification_receipt"] = str(
        tmp_path / "week03-verification-receipt.json"
    )
    comparison["week03_run_status"] = str(tmp_path / "week03-run-status.json")
    comparison["arrival_trace_dir"] = str(week03_root / "raw" / "traces")
    comparison["max_workers"] = 2
    comparison["max_p99_arrival_lag_ms"] = 1_000
    return value


def trace_rows() -> tuple[TraceRequest, ...]:
    return (
        TraceRequest(0, "req-warmup", 0, 256, 64, "fixed", False),
        TraceRequest(1, "req-measure-1", 1_000_000, 256, 64, "fixed", True),
        TraceRequest(2, "req-measure-2", 2_000_000, 256, 64, "fixed", True),
    )


def write_test_trace(tmp_path):
    path = tmp_path / "traces" / "balanced.csv"
    write_trace(path, trace_rows())
    return path


def completed_stream(_url, payload, _timeout, _api_key):
    prompt = payload["prompt"]
    yield SimpleNamespace(
        done=False,
        usage={"prompt_tokens": len(prompt), "completion_tokens": 64},
        choices=(SimpleNamespace(content="token", finish_reason="length"),),
    )
    yield SimpleNamespace(done=True, usage=None, choices=())


def short_output_stream(_url, payload, _timeout, _api_key):
    yield SimpleNamespace(
        done=False,
        usage={"prompt_tokens": len(payload["prompt"]), "completion_tokens": 63},
        choices=(SimpleNamespace(content="token", finish_reason="stop"),),
    )
    yield SimpleNamespace(done=True, usage=None, choices=())


def test_plan_preserves_every_trace_field_and_exact_file_identity(tmp_path) -> None:
    cfg = config(tmp_path)
    path = write_test_trace(tmp_path)

    plan = plan_trace(cfg, path, request_rate=4.0, repeat=1, max_workers=2)

    assert plan["trace_id"] == trace_fingerprint(trace_rows())
    assert len(plan["trace_file_sha256"]) == 64
    assert plan["warmup_request_count"] == 1
    assert plan["measurement_request_count"] == 2
    assert [row["request_id"] for row in plan["requests"]] == [
        request.request_id for request in trace_rows()
    ]
    assert [row["scheduled_arrival_ns"] for row in plan["requests"]] == [
        request.scheduled_arrival_ns for request in trace_rows()
    ]
    assert [row["measurement"] for row in plan["requests"]] == [False, True, True]
    assert plan["equivalence_contract"]["identical_prompt_token_ids"] is True
    assert plan["equivalence_contract"]["byte_identical_prompt_claimed"] is True


def test_exact_prompt_shape_is_deterministic_and_cpu_only(tmp_path) -> None:
    cfg = config(tmp_path)
    request = trace_rows()[1]
    torch_before = sys.modules.get("torch")

    first = deterministic_prompt_token_ids(FakeTokenizer(), request, "base prompt")
    second = deterministic_prompt_token_ids(FakeTokenizer(), request, "base prompt")
    payload = completion_payload(cfg, request, first)

    assert first == second
    assert len(first) == request.prompt_tokens == 256
    assert payload["prompt"] == first
    assert payload["max_tokens"] == request.output_tokens == 64
    assert payload["ignore_eos"] is True
    assert sys.modules.get("torch") is torch_before


def test_execute_trace_retains_warmup_rows_but_excludes_them_from_aggregates(
    tmp_path,
) -> None:
    cfg = config(tmp_path)
    path = write_test_trace(tmp_path)
    metadata = create_run_metadata(cfg, make_source(), make_runtime())

    result = execute_trace(
        cfg,
        path,
        1,
        request_rate=4.0,
        repeat=0,
        metadata=metadata,
        server_instance_id=SERVER_INSTANCE_ID,
        server_attempt_id=SERVER_ATTEMPT_ID,
        tokenizer=FakeTokenizer(),
        stream_factory=completed_stream,
        max_p99_arrival_lag_ms=1_000,
    )

    validate_artifact_identity(result, metadata, "trace replay")
    assert result["trace"]["trace_id"] == trace_fingerprint(trace_rows())
    assert result["server_instance_id"] == SERVER_INSTANCE_ID
    assert result["server_attempt_id"] == SERVER_ATTEMPT_ID
    assert result["case"]["concurrency"] is None
    assert result["counts"] == {
        "requested": 2,
        "success": 2,
        "timeout": 0,
        "error": 0,
    }
    assert result["execution"]["all_requests"]["requested"] == 3
    assert result["execution"]["warmup_requests"]["requested"] == 1
    assert result["execution"]["max_observed_in_flight"] <= 1
    assert result["execution"]["arrival_lag_gate"]["passed"] is True
    assert [row["status"] for row in result["records"]] == [
        "completed",
        "completed",
        "completed",
    ]
    assert all(row["arrival_lag_ns"] >= 0 for row in result["records"])
    assert all(
        row["arrival_lag_ns"] == row["request_started_ns"] - row["scheduled_arrival_ns"]
        for row in result["records"]
    )
    assert all(
        row["submission_lag_ns"] == row["submitted_ns"] - row["scheduled_arrival_ns"]
        for row in result["records"]
    )
    assert all(row["prompt_shape_match"] is True for row in result["records"])
    assert all(row["output_shape_match"] is True for row in result["records"])
    assert result["equivalence_contract"]["identical_prompt_token_ids"] is True
    assert result["equivalence_contract"]["byte_identical_prompt_claimed"] is True


def test_trace_replay_rejects_base_prompt_different_from_week03(tmp_path) -> None:
    cfg = config(tmp_path)
    cfg["benchmark"]["comparison"]["base_prompt"] = "different prompt"
    path = write_test_trace(tmp_path)

    with pytest.raises(ValueError, match="base_prompt differs"):
        plan_trace(cfg, path, request_rate=4.0, repeat=0, max_workers=2)


def test_execute_trace_treats_short_output_as_terminal_error(tmp_path) -> None:
    cfg = config(tmp_path)
    path = write_test_trace(tmp_path)
    metadata = create_run_metadata(cfg, make_source(), make_runtime())

    result = execute_trace(
        cfg,
        path,
        2,
        request_rate=4.0,
        metadata=metadata,
        server_instance_id=SERVER_INSTANCE_ID,
        server_attempt_id=SERVER_ATTEMPT_ID,
        tokenizer=FakeTokenizer(),
        stream_factory=short_output_stream,
        max_p99_arrival_lag_ms=1_000,
    )

    assert result["counts"]["success"] == 0
    assert result["counts"]["error"] == 2
    assert all(
        row["error_type"] == "OutputTokenCountMismatch" for row in result["records"]
    )
    assert all(row["output_shape_match"] is False for row in result["records"])


def test_plan_rejects_trace_bytes_tampered_without_semantic_id_update(tmp_path) -> None:
    cfg = config(tmp_path)
    path = write_test_trace(tmp_path)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(",256,64,fixed,true", ",128,64,fixed,true", 1), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="trace_id does not match trace contents"):
        plan_trace(cfg, path, request_rate=4.0, repeat=0, max_workers=2)


def test_all_configured_manifest_and_records_keep_week04_identity(
    tmp_path, monkeypatch
) -> None:
    cfg = config(tmp_path)
    path = write_test_trace(tmp_path)
    metadata = create_run_metadata(cfg, make_source(), make_runtime())
    week03_metadata = tmp_path / "week03-metadata.json"
    week03_events = tmp_path / "week03-events.csv"
    week03_summary = tmp_path / "week03-summary.csv"
    week03_receipt = tmp_path / "week03-verification-receipt.json"
    week03_status = tmp_path / "week03-run-status.json"
    week03_metadata.write_text("{}\n", encoding="utf-8")
    week03_events.write_text("events\n", encoding="utf-8")
    week03_summary.write_text("summary\n", encoding="utf-8")
    week03_receipt.write_text("{}\n", encoding="utf-8")
    week03_status.write_text("{}\n", encoding="utf-8")
    case = {
        "trace_id": trace_fingerprint(trace_rows()),
        "trace_path": str(path),
        "trace_file_sha256": trace_client.sha256_file(path),
        "request_rate": 4.0,
        "repeat": 0,
        "measurement_start_ns": 1_000_000,
        "measurement_end_ns": 3_000_000,
    }
    monkeypatch.setattr(trace_client, "configured_replay_cases", lambda _config: [case])

    manifest = run_all_configured(
        cfg,
        metadata=metadata,
        server_instance_id=SERVER_INSTANCE_ID,
        server_attempt_id=SERVER_ATTEMPT_ID,
        tokenizer=FakeTokenizer(),
        stream_factory=completed_stream,
    )

    validate_artifact_identity(manifest, metadata, "comparison manifest")
    assert manifest["server_instance_id"] == SERVER_INSTANCE_ID
    assert manifest["server_attempt_id"] == SERVER_ATTEMPT_ID
    validate_artifact_identity(manifest["records"][0], metadata, "manifest record")
    assert manifest["case_count"] == 1
    assert manifest["records"][0]["trace"]["trace_id"] == case["trace_id"]
    assert manifest["records"][0]["execution"]["max_workers"] == 2
    assert manifest["records"][0]["server_instance_id"] == SERVER_INSTANCE_ID
    assert manifest["records"][0]["server_attempt_id"] == SERVER_ATTEMPT_ID
    assert manifest["artifacts"][0]["sha256"] == trace_client.sha256_file(
        manifest["artifacts"][0]["path"]
    )
    resumed = run_all_configured(
        cfg,
        metadata=metadata,
        server_instance_id=SERVER_INSTANCE_ID,
        server_attempt_id=SERVER_ATTEMPT_ID,
        tokenizer=FakeTokenizer(),
        stream_factory=lambda *_args: pytest.fail("completed replay should be reused"),
    )
    assert resumed["records"] == manifest["records"]
    replay_path = Path(manifest["artifacts"][0]["path"])
    tampered = json.loads(replay_path.read_text())
    tampered["metrics"]["duration_seconds"] *= 2
    replay_path.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="aggregate metrics are inconsistent"):
        run_all_configured(
            cfg,
            metadata=metadata,
            server_instance_id=SERVER_INSTANCE_ID,
            server_attempt_id=SERVER_ATTEMPT_ID,
            tokenizer=FakeTokenizer(),
            stream_factory=lambda *_args: pytest.fail("invalid replay must not be rerun silently"),
        )
    assert set(manifest["week03_inputs"]) == {
        "config",
        "run_metadata",
        "events",
        "summary",
        "verification_receipt",
        "run_status",
        "arrival_trace_dir",
    }


def test_week03_gate_requires_completed_receipt_bound_to_fresh_verification(
    tmp_path, monkeypatch
) -> None:
    cfg = config(tmp_path)
    comparison = cfg["benchmark"]["comparison"]
    week03_config = trace_client.load_yaml(comparison["week03_config"])
    summary = {
        "run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "profile": "primary",
        "backend": "hf",
        "metadata_fingerprint": "m" * 64,
        "calibration_id": "calibration",
        "case_count": 55,
        "event_count": 123,
        "batch_count": 45,
        "artifact_sha256": {"events": "e" * 64, "run_status": "s" * 64},
    }
    monkeypatch.setattr("scripts.verify_week03.verify", lambda _config_path: summary)
    trace_client.write_json(
        comparison["week03_verification_receipt"],
        {
            "schema_version": 2,
            "status": "completed",
            "verified_at": "2026-01-01T00:00:00+00:00",
            **summary,
            "artifact_sha256": {"events": "e" * 64, "run_status": "old"},
        },
    )
    trace_client.write_json(
        comparison["week03_run_status"],
        {
            "status": "completed",
            "exit_code": 0,
            "run_id": summary["run_id"],
            "verification_receipt": comparison["week03_verification_receipt"],
        },
    )

    verified, receipt, state = _read_week03_verification(comparison, week03_config)

    assert verified == summary
    assert receipt["status"] == "completed"
    assert len(state["case_ids"]) == 55
    assert state["case_ids"] == {
        week03_case_id(case) for case in expand_matrix(week03_config, "primary")
    }

    status = trace_client.read_json(comparison["week03_run_status"])
    status["status"] = "artifacts_ready"
    trace_client.write_json(comparison["week03_run_status"], status)
    with pytest.raises(ValueError, match="completed verification"):
        _read_week03_verification(comparison, week03_config)


def test_load_ready_server_instance_binds_week04_identity(tmp_path) -> None:
    cfg = config(tmp_path)
    metadata = create_run_metadata(cfg, make_source(), make_runtime())
    server_path = tmp_path / "server-process.json"
    cfg["server"]["pid_metadata_path"] = str(server_path)
    from src.common import write_json
    from src.week04_contract import artifact_identity

    write_json(
        server_path,
        {
            **artifact_identity(metadata),
            "state": "ready",
            "server_instance_id": SERVER_INSTANCE_ID,
            "server_attempt_id": SERVER_ATTEMPT_ID,
        },
    )

    assert (
        trace_client.load_ready_server_instance_id(cfg, metadata) == SERVER_INSTANCE_ID
    )
    assert trace_client.load_ready_server_identity(cfg, metadata) == {
        "server_instance_id": SERVER_INSTANCE_ID,
        "server_attempt_id": SERVER_ATTEMPT_ID,
    }

    payload = trace_client.read_json(server_path)
    payload["run_id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    write_json(server_path, payload)
    with pytest.raises(ValueError, match="identity mismatch for run_id"):
        trace_client.load_ready_server_instance_id(cfg, metadata)
