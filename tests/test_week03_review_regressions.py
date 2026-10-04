from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from scripts.prepare_week03_run import effective_config
from src.analyze_week03 import baseline_summaries, summarize_case, summary_csv_text
from src.analyze_week04 import load_week03_records
from src.common import load_yaml
from src.openai_trace_client import summarize_records
from src.serve_week03 import FakeBatchBackend, simulate_case
from src.week03_contract import CaseKey, create_run_metadata, expand_matrix
from src.workload import TraceRequest
from tests.test_week03_analysis import evidence


@pytest.mark.parametrize("service_ms", [40, 100_000])
def test_replay_and_week03_use_identical_windows_with_and_without_drain(service_ms):
    config = load_yaml("configs/week03.yaml")
    case = CaseKey("primary", "0.75", 0, "no_batching", 1, 0)
    metadata = create_run_metadata(config, profile="primary", backend="fake")
    trace = tuple(
        TraceRequest(i, f"r{i}", (20 + i) * 10**9, 256, 64, measurement=True) for i in range(3)
    )
    events, batches = simulate_case(
        trace=trace,
        case=case,
        config=config,
        metadata=metadata,
        backend=FakeBatchBackend({1: service_ms}),
        arrival_rate_rps=18.75,
    )
    week03 = summarize_case(events, batches, metadata=metadata)
    replay_rows = [
        {
            "measurement": True,
            "status": row["status"],
            "scheduled_arrival_ns": row["scheduled_arrival_ns"],
            "terminal_ns": row["terminal_ns"],
            "arrival_lag_ns": 0,
            "actual_prompt_tokens": 256,
            "actual_output_tokens": 64,
        }
        for row in events
    ]
    replay = summarize_records(
        replay_rows,
        max_p99_arrival_lag_ms=100,
        measurement_start_ns=20 * 10**9,
        measurement_end_ns=120 * 10**9,
    )["metrics"]
    assert replay["request_throughput"] == week03["achieved_throughput_rps"] == 0.03
    assert replay["duration_seconds"] == week03["measurement_duration_seconds"] == 100
    assert replay["drain_duration_seconds"] == week03["drain_duration_seconds"]
    assert replay["drain_inclusive_throughput_rps"] == week03["drain_inclusive_throughput_rps"]
    assert replay["drain_duration_seconds"] >= 100


def test_additional_sweep_cases_do_not_change_fixed_parameter_baselines(tmp_path):
    config = load_yaml("configs/week03.yaml")
    rows = [
        {
            "policy": case.policy,
            "max_batch_size": case.max_batch_size,
            "delay_ms": case.delay_ms,
            "repeat": case.repeat,
            "offered_load_ratio": case.ratio,
        }
        for case in expand_matrix(config, "primary")
    ]
    baseline = baseline_summaries(pd.DataFrame(rows), config)
    assert len(baseline) == 45
    assert set(baseline.groupby(["policy", "offered_load_ratio"]).size()) == {3}
    extended = [*rows, {**rows[0], "max_batch_size": 99, "delay_ms": 999}]
    pd.testing.assert_frame_equal(baseline, baseline_summaries(pd.DataFrame(extended), config))

    summaries = []
    for size, delay in ((8, 10), (4, 2)):
        events, batches, metadata = evidence("size_or_time", "0.75", delay, size)
        summaries.append(summarize_case(events, batches, metadata=metadata))
    path = tmp_path / "summary.csv"
    path.write_text(summary_csv_text(summaries))
    records = load_week03_records(path, config)
    assert len(records) == 1
    assert records[0]["case"]["max_batch_size"] == 8
    assert records[0]["case"]["delay_ms"] == 10


def isolated_source_config(tmp_path):
    config = load_yaml("configs/week03.yaml")
    root = tmp_path / "official"
    for key, value in config["output"].items():
        if value == "results/week03" or value.startswith("results/week03/"):
            config["output"][key] = str(root / value.removeprefix("results/week03").lstrip("/"))
    config["calibration"]["artifact"] = str(root / "calibration.json")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return config, path


@pytest.mark.parametrize("backend", ["fake", "hf"])
def test_smoke_entrypoint_is_complete_and_preserves_official_artifacts(tmp_path, backend):
    config, config_path = isolated_source_config(tmp_path)
    preserved = []
    for key in ("environment_json", "dependency_freeze", "model_snapshot", "run_log"):
        path = Path(config["output"][key])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"official evidence\n")
        preserved.append(path)
    calibration = Path(config["calibration"]["artifact"])
    calibration.write_bytes(b"official calibration\n")
    preserved.append(calibration)
    before = {path: path.read_bytes() for path in preserved}
    root = tmp_path / backend
    environment = {**os.environ, "MPLBACKEND": "Agg", "MPLCONFIGDIR": str(tmp_path / "mpl")}
    environment.pop("RUN_LOG", None)
    environment.pop("MAX_CASES", None)
    if backend == "fake":
        environment.update(
            PYTHON=sys.executable,
            CONFIG=str(config_path),
            PROFILE="smoke",
            BACKEND="fake",
            OUTPUT_ROOT=str(root),
            CALIBRATE="0",
        )
        argv = ["bash", "scripts/run_week03.sh"]
    else:
        # Exercise the real Make/shell preflight order while substituting hardware
        # and model work. The runner and analyzer still execute against real files.
        wrapper = tmp_path / "python-wrapper"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            + """
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, os.getcwd())
from src.common import load_yaml
args = sys.argv[1:]
module = args[1] if args[:1] == ['-m'] else ''
if module == 'scripts.prepare_week01_model' and os.environ.get('TEST_PREFLIGHT_FAIL'):
    print('injected model preparation failure')
    sys.exit(17)
if module == 'pip':
    print('test-freeze==1')
    sys.exit(0)
outputs = {'scripts.prepare_week01_model': 'model_snapshot', 'scripts.check_env': 'environment_json'}
if module in outputs or module == 'src.week03_calibration':
    flag = '--week3-config' if module == 'src.week03_calibration' else '--config'
    config = load_yaml(args[args.index(flag) + 1])
    target = config['calibration']['artifact'] if module == 'src.week03_calibration' else config['output'][outputs[module]]
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'test_preflight': module}))
    sys.exit(0)
if module == 'src.serve_week03':
    args[args.index('--backend') + 1] = 'fake'
sys.exit(subprocess.call([sys.executable, *args]))
"""
        )
        wrapper.chmod(0o755)
        argv = [
            "make",
            "smoke-week03",
            f"PYTHON={wrapper}",
            f"WEEK03_CONFIG={config_path}",
            f"WEEK03_SMOKE_ROOT={root}",
        ]
    result = subprocess.run(argv, env=environment, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert {path: path.read_bytes() for path in preserved} == before
    analysis = json.loads((root / "analysis.json").read_text())
    assert analysis["case_count"] == 3
    assert analysis["figures"] == []
    assert (root / "summary.csv").is_file()
    assert (root / "logs/week03.log").is_file()
    if backend == "hf":
        assert (root / "environment.json").is_file()
        assert (root / "model-snapshot.json").is_file()
        assert (root / "dependency-freeze.txt").read_text() == "test-freeze==1\n"
        assert (root / "calibration.json").is_file()
        failed_root = tmp_path / "failed-smoke"
        failed = subprocess.run(
            [*argv[:-1], f"WEEK03_SMOKE_ROOT={failed_root}"],
            env={**environment, "TEST_PREFLIGHT_FAIL": "1"},
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert failed.returncode != 0
        assert "injected model preparation failure" in (failed_root / "logs/week03.log").read_text()
        assert not (failed_root / "raw/events.csv").exists()
        assert {path: path.read_bytes() for path in preserved} == before


def test_nonofficial_run_cannot_target_official_root_or_log(tmp_path):
    config, _ = isolated_source_config(tmp_path)
    with pytest.raises(ValueError, match="outside the official root"):
        effective_config(
            config, profile="smoke", backend="hf", output_root=config["output"]["root"]
        )
    with pytest.raises(ValueError, match="isolated run log"):
        effective_config(
            config,
            profile="smoke",
            backend="hf",
            output_root=str(tmp_path / "smoke"),
            run_log=config["output"]["run_log"],
        )
