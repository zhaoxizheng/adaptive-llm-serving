import csv
from pathlib import Path

import pytest

from src.analyze_week05 import analyze_directory, analyze_run
from src.analyze_week06 import confirm_cell
from src.common import load_yaml, read_json, write_json
from src.study_contract import fingerprint


def write_run(root, config, rate, repeat, *, growing=False):
    root.mkdir(parents=True)
    rid = root.name
    source = {"source_files": {"src/study_runner.py": "fixture"}}
    meta = {
        "run_id": rid,
        "status": "completed",
        "config": config,
        "config_fingerprint": fingerprint(config),
        "server": {
            "argv": ["vllm", "serve", "fixture"],
            "runtime": {"gpu": "fixture"},
            "source": source,
        },
        "mixture": "short_chat",
        "offered_rps": rate,
        "repeat": repeat,
        "tags": {},
        "measurement_start": 100,
        "measurement_end": 160,
        "gpu_errors": [],
    }
    write_json(root / "metadata.json", meta)
    rows = [
        {
            "request_id": f"{rid}-{i}",
            "workload": "short_chat",
            "status": "success",
            "scheduled_at": 105 + i * 15,
            "completed_at": 105.5 + i * 15,
            "first_content_at": 105.1 + i * 15,
            "last_content_at": 105.41 + i * 15,
            "arrival_lag_ms": 0,
            "ttft_ms": 100,
            "tpot_ms": 10,
            "e2e_ms": 500,
            "prompt_tokens": 128,
            "output_tokens": 32,
            "usage": {"prompt_tokens": 128, "completion_tokens": 32},
        }
        for i in range(3)
    ]
    write_json(root / "client.json", rows)
    queries = {}
    for name in ("waiting", "running", "kv_usage", "prompt_tokens", "generation_tokens"):
        queries[name] = {
            "values": [
                [t, (t - 100) / 10 if growing and name == "waiting" else 0.2]
                for t in range(100, 161, 5)
            ]
        }
    write_json(
        root / "prometheus.json",
        {"run_id": rid, "start": 100, "end": 160, "queries": queries, "errors": []},
    )
    with (root / "gpu.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "utilization_pct", "memory_used_mib"])
        writer.writerows([[t, 50, 1000] for t in range(100, 161, 5)])
    (root / "server.log").write_text("fixture server log\n")
    return root


def test_full_offline_capacity_handoff_and_figures(tmp_path, monkeypatch):
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "matplotlib"))
    config = load_yaml("configs/week05.yaml")
    config["calibration"].update(confirmed=True, baseline_rps=1, evidence="fixture")
    config["mixtures"] = {"short_chat": {"short_chat": 1.0}}
    config["load"]["multipliers"] = [0.25, 0.5, 1.0]
    for rate in config["load"]["multipliers"]:
        for repeat in range(3):
            write_run(
                tmp_path / "raw" / "sessions" / "s" / "runs" / f"{rate}-{repeat}",
                config,
                rate,
                repeat,
                growing=rate == 1.0,
            )
    result = analyze_directory(tmp_path)
    assert result["capacity"]["short_chat"]["max_stable_rps"] == 0.5
    assert result["capacity"]["short_chat"]["first_unstable_rps"] == 1.0
    handoff = read_json(tmp_path / "handoff.json")
    assert handoff["ready"]
    assert handoff["load_points"]["short_chat"] == {"low": 0.25, "boundary": 0.5, "overload": 1}
    assert len(list((tmp_path / "figures").glob("*.png"))) == 5


def test_failures_and_drain_do_not_disappear_from_capacity(tmp_path):
    config = load_yaml("configs/week05.yaml")
    root = write_run(tmp_path / "run", config, 1, 0)
    rows = read_json(root / "client.json")
    rows[0].update(status="timeout", ttft_ms=None, tpot_ms=None)
    rows[1].update(
        scheduled_at=159.9, first_content_at=160.0, last_content_at=160.31, completed_at=160.4
    )
    write_json(root / "client.json", rows)
    result = analyze_run(root)
    assert result["valid"] and not result["stable"]
    assert result["error_rate"] == pytest.approx(1 / 3)
    assert result["late_completions"] == 1
    assert result["goodput_rps"] == pytest.approx(2 / 60)
    assert result["completed_goodput_rps"] == pytest.approx(1 / 60)
    assert result["achieved_rps"] == pytest.approx(1 / 60)


def test_confirmation_rejects_incomplete_soak_and_missing_headroom(tmp_path):
    config = load_yaml("configs/week06.yaml")
    cell = {
        "runs": [],
        "repeats": 3,
        "quality_passed": True,
        "manual_review": "passed",
        "status": "completed",
    }
    for repeat in range(3):
        cell["runs"].append(
            {
                "repeat": repeat,
                "stable": True,
                "kv_usage_peak": 0.5,
                "tags": {"load_point": "boundary"},
            }
        )
    reasons = confirm_cell(cell, config)
    assert reasons == ["stable soak evidence missing"]
    write_json(tmp_path / "metadata.json", {"measurement_start": 100, "measurement_end": 160})
    cell["runs"].append(
        {
            "stable": True,
            "kv_usage_peak": 0.99,
            "path": str(tmp_path),
            "tags": {"load_point": "boundary", "soak": True},
        }
    )
    reasons = confirm_cell(cell, config)
    assert "soak shorter than configured duration" in reasons
    assert "insufficient measured KV-cache headroom" in reasons


def test_identity_drift_cannot_be_merged_as_one_capacity_sweep(tmp_path):
    config = load_yaml("configs/week05.yaml")
    first = write_run(tmp_path / "raw/sessions/s/runs/a", config, 1, 0)
    second = write_run(tmp_path / "raw/sessions/s/runs/b", config, 2, 0)
    meta = read_json(second / "metadata.json")
    meta["server"]["runtime"]["gpu"] = "different GPU"
    write_json(second / "metadata.json", meta)
    assert Path(first).is_dir()
    with pytest.raises(ValueError, match="mixed experiment identities"):
        analyze_directory(tmp_path, figures=False)


def test_stored_latency_cannot_override_raw_event_times(tmp_path):
    config = load_yaml("configs/week05.yaml")
    root = write_run(tmp_path / "run", config, 1, 0)
    rows = read_json(root / "client.json")
    rows[0]["ttft_ms"] = 1
    write_json(root / "client.json", rows)
    result = analyze_run(root)
    assert not result["valid"]
    assert any("disagrees with timestamps" in reason for reason in result["invalid_reasons"])


def test_week06_failed_variant_does_not_hide_verified_fallback(tmp_path, monkeypatch):
    from src import analyze_week06

    config = load_yaml("configs/week06.yaml")
    handoff = {"week05_config": {"load": {"repeats": 3}}}
    for role in ("candidate", "fallback", "failed"):
        session = tmp_path / "raw/sessions" / role
        tags = {
            "phase": "confirm" if role != "failed" else "representation",
            "role": role,
            "variant": "awq" if role != "fallback" else "bf16",
            "overrides": {},
        }
        write_json(
            session / "experiment.json",
            {
                "config": config,
                "handoff": handoff,
                "tags": tags,
                "status": "failed" if role == "failed" else "completed",
            },
        )
        if role == "failed":
            continue
        write_json(
            session / "server.json",
            {"argv": ["vllm", "serve", role], "runtime": {"gpu": "fixture"}, "source": {}},
        )
        write_json(
            session / "quality.json",
            {"passed": True, "manual_review": "passed", "suite_fingerprint": "same-fixture"},
        )
        for point in ("boundary", "overload"):
            for repeat in range(3):
                path = session / "runs" / f"{point}-{repeat}"
                write_json(
                    path / "metadata.json", {"measurement_start": 100, "measurement_end": 160}
                )
                write_json(
                    path / "fixture-summary.json",
                    {
                        "repeat": repeat,
                        "valid": True,
                        "stable": True,
                        "mixture": "mixed",
                        "kv_usage_peak": 0.5,
                        "goodput_rps": 2 if role == "candidate" and point == "overload" else 1,
                        "path": str(path),
                        "tags": {"load_point": point},
                    },
                )
        path = session / "runs/soak"
        write_json(path / "metadata.json", {"measurement_start": 100, "measurement_end": 1300})
        write_json(
            path / "fixture-summary.json",
            {
                "repeat": 0,
                "valid": True,
                "stable": True,
                "mixture": "mixed",
                "kv_usage_peak": 0.5,
                "goodput_rps": 1,
                "path": str(path),
                "tags": {"load_point": "boundary", "soak": True},
            },
        )
    monkeypatch.setattr(
        analyze_week06, "analyze_run", lambda p: read_json(p / "fixture-summary.json")
    )
    result = analyze_week06.analyze(tmp_path, figures=False)
    assert result["comparable"]
    assert result["operating_point"] == ["vllm", "serve", "candidate"]
    assert result["fallback"] == ["vllm", "serve", "fallback"]
    assert len(result["cells"]) == 3
