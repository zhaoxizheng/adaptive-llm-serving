from __future__ import annotations

import signal
import uuid
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import start_vllm
from src.week04_contract import artifact_identity, create_run_metadata
from tests.test_vllm_contract import make_config
from tests.test_week04_contract import make_runtime, make_source


def make_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "pid_metadata": tmp_path / "server-process.json",
        "run_metadata": tmp_path / "run-metadata.json",
    }


def make_process_metadata(
    config: dict[str, object], *, pid: int = 41001
) -> tuple[dict[str, object], dict[str, object]]:
    run_metadata = create_run_metadata(config, make_source(), make_runtime())
    process_metadata: dict[str, object] = {
        "pid": pid,
        "pgid": pid,
        "session_id": pid,
        "start_identity": "fake-start",
        "process_argv": ["fake-vllm", "serve"],
        "state": "ready",
        "server_instance_id": "12345678-1234-4234-8234-123456789abc",
        "server_attempt_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "settings": {
            "host": config["server"]["host"],  # type: ignore[index]
            "port": config["server"]["port"],  # type: ignore[index]
        },
        **artifact_identity(run_metadata),
    }
    return run_metadata, process_metadata


def write_metadata(
    paths: dict[str, Path],
    run_metadata: dict[str, object],
    process_metadata: dict[str, object],
) -> None:
    start_vllm.atomic_write_json(paths["run_metadata"], run_metadata)
    start_vllm.atomic_write_json(paths["pid_metadata"], process_metadata)


def test_signal_owned_process_group_targets_group_not_leader(monkeypatch) -> None:
    pid = 41001
    metadata = {
        "pid": pid,
        "pgid": pid,
        "session_id": pid,
        "start_identity": "fake-start",
        "process_argv": ["fake-vllm"],
    }
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(start_vllm, "current_process_group", lambda: 999)
    monkeypatch.setattr(
        start_vllm, "ownership_status", lambda value: ("owned_running", "match")
    )
    monkeypatch.setattr(start_vllm, "process_group_id", lambda value: pid)
    monkeypatch.setattr(start_vllm, "process_session_id", lambda value: pid)
    monkeypatch.setattr(start_vllm, "group_exists", lambda value: True)
    monkeypatch.setattr(
        start_vllm.os, "killpg", lambda pgid, signum: signals.append((pgid, signum))
    )

    start_vllm.signal_owned_process_group(metadata, signal.SIGTERM, require_leader=True)

    assert signals == [(pid, signal.SIGTERM)]


def test_signal_owned_process_group_refuses_own_or_unowned_group(monkeypatch) -> None:
    metadata = {"pid": 41001, "pgid": 41001, "session_id": 41001}
    monkeypatch.setattr(start_vllm, "current_process_group", lambda: 41001)
    monkeypatch.setattr(
        start_vllm.os,
        "killpg",
        lambda *_: pytest.fail("must never signal the controller group"),
    )

    with pytest.raises(start_vllm.VllmScriptError, match="own process group"):
        start_vllm.signal_owned_process_group(
            metadata, signal.SIGTERM, require_leader=False
        )

    monkeypatch.setattr(start_vllm, "current_process_group", lambda: 999)
    metadata["pgid"] = 41002
    with pytest.raises(start_vllm.VllmScriptError, match="not created as an owned"):
        start_vllm.signal_owned_process_group(
            metadata, signal.SIGTERM, require_leader=False
        )


def test_stop_signals_group_and_waits_for_group_and_port(
    tmp_path: Path, monkeypatch
) -> None:
    config = make_config()
    config["server"]["shutdown_timeout_seconds"] = 1  # type: ignore[index]
    paths = make_paths(tmp_path)
    run_metadata, process_metadata = make_process_metadata(config)
    write_metadata(paths, run_metadata, process_metadata)
    signals: list[tuple[signal.Signals, bool]] = []
    waited: list[tuple[int, str, int, float]] = []

    monkeypatch.setattr(start_vllm, "current_process_group", lambda: 999)
    monkeypatch.setattr(
        start_vllm, "ownership_status", lambda value: ("owned_running", "match")
    )
    monkeypatch.setattr(start_vllm, "process_group_id", lambda value: 41001)
    monkeypatch.setattr(start_vllm, "process_session_id", lambda value: 41001)
    monkeypatch.setattr(
        start_vllm,
        "signal_owned_process_group",
        lambda metadata, signum, *, require_leader: signals.append(
            (signum, require_leader)
        ),
    )

    def wait(pgid: int, host: str, port: int, timeout: float):
        waited.append((pgid, host, port, timeout))
        return False, False, True

    monkeypatch.setattr(start_vllm, "wait_for_group_and_port_closed", wait)

    result = start_vllm.stop_server(config, paths)

    assert result == {"status": "stopped", "pid": 41001, "pgid": 41001}
    assert signals == [(signal.SIGTERM, True)]
    assert waited == [(41001, "127.0.0.1", 8000, 1.0)]
    stored = start_vllm.read_metadata(paths["pid_metadata"])
    assert stored is not None and stored["state"] == "stopped"


def test_stop_rejects_port_held_after_leader_and_group_exit(
    tmp_path: Path, monkeypatch
) -> None:
    config = make_config()
    paths = make_paths(tmp_path)
    run_metadata, process_metadata = make_process_metadata(config)
    write_metadata(paths, run_metadata, process_metadata)
    monkeypatch.setattr(start_vllm, "current_process_group", lambda: 999)
    monkeypatch.setattr(
        start_vllm, "ownership_status", lambda value: ("not_running", "gone")
    )
    monkeypatch.setattr(start_vllm, "group_exists", lambda value: False)
    monkeypatch.setattr(start_vllm, "port_is_open", lambda host, port: True)
    monkeypatch.setattr(
        start_vllm.os,
        "killpg",
        lambda *_: pytest.fail("must not signal after ownership is gone"),
    )

    with pytest.raises(start_vllm.VllmScriptError, match="port is still held"):
        start_vllm.stop_server(config, paths)

    stored = start_vllm.read_metadata(paths["pid_metadata"])
    assert stored is not None and stored["state"] == "ready"


def test_stop_rejects_port_replacement_after_group_disappears(
    tmp_path: Path, monkeypatch
) -> None:
    config = make_config()
    paths = make_paths(tmp_path)
    run_metadata, process_metadata = make_process_metadata(config)
    write_metadata(paths, run_metadata, process_metadata)
    monkeypatch.setattr(start_vllm, "current_process_group", lambda: 999)
    monkeypatch.setattr(
        start_vllm, "ownership_status", lambda value: ("owned_running", "match")
    )
    monkeypatch.setattr(start_vllm, "process_group_id", lambda value: 41001)
    monkeypatch.setattr(start_vllm, "process_session_id", lambda value: 41001)
    signals: list[signal.Signals] = []
    monkeypatch.setattr(
        start_vllm,
        "signal_owned_process_group",
        lambda metadata, signum, *, require_leader: signals.append(signum),
    )
    monkeypatch.setattr(
        start_vllm,
        "wait_for_group_and_port_closed",
        lambda *args: (False, True, True),
    )

    with pytest.raises(start_vllm.VllmScriptError, match="may now belong"):
        start_vllm.stop_server(config, paths, timeout_override=0.1)

    assert signals == [signal.SIGTERM]


def test_stop_escalates_surviving_owned_group_and_preserves_failure(
    tmp_path: Path, monkeypatch
) -> None:
    config = make_config()
    paths = make_paths(tmp_path)
    run_metadata, process_metadata = make_process_metadata(config)
    write_metadata(paths, run_metadata, process_metadata)
    monkeypatch.setattr(start_vllm, "current_process_group", lambda: 999)
    monkeypatch.setattr(
        start_vllm, "ownership_status", lambda value: ("owned_running", "match")
    )
    monkeypatch.setattr(start_vllm, "process_group_id", lambda value: 41001)
    monkeypatch.setattr(start_vllm, "process_session_id", lambda value: 41001)
    signals: list[tuple[signal.Signals, bool]] = []
    monkeypatch.setattr(
        start_vllm,
        "signal_owned_process_group",
        lambda metadata, signum, *, require_leader: signals.append(
            (signum, require_leader)
        ),
    )
    waits = iter(((True, True, False), (False, False, True)))
    monkeypatch.setattr(
        start_vllm, "wait_for_group_and_port_closed", lambda *args: next(waits)
    )

    result = start_vllm.stop_server(
        config, paths, timeout_override=0.1, failure_context="readiness timed out"
    )

    assert result["status"] == "stopped"
    assert signals == [(signal.SIGTERM, True), (signal.SIGKILL, False)]
    stored = start_vllm.read_metadata(paths["pid_metadata"])
    assert stored is not None
    assert stored["readiness_error"] == "readiness timed out"


def test_stop_rejects_mixed_week04_run_without_signaling(
    tmp_path: Path, monkeypatch
) -> None:
    config = make_config()
    paths = make_paths(tmp_path)
    current_run, process_metadata = make_process_metadata(config)
    different_run = create_run_metadata(config, make_source(), make_runtime())
    assert current_run["run_id"] != different_run["run_id"]
    write_metadata(paths, different_run, process_metadata)
    monkeypatch.setattr(
        start_vllm.os,
        "killpg",
        lambda *_: pytest.fail("must not signal a different Week 4 run"),
    )

    with pytest.raises(start_vllm.VllmScriptError, match="current Week 4 run"):
        start_vllm.stop_server(config, paths)


def test_require_same_week04_run_rejects_config_or_runtime_change(
    tmp_path: Path, monkeypatch
) -> None:
    config = make_config()
    paths = make_paths(tmp_path)
    run_metadata, process_metadata = make_process_metadata(config)
    start_vllm.atomic_write_json(paths["run_metadata"], run_metadata)

    changed_config = deepcopy(config)
    changed_config["server"]["max_num_seqs"] = 7  # type: ignore[index]
    with pytest.raises(start_vllm.VllmScriptError, match="valid Week 4 run metadata"):
        start_vllm.require_same_week04_run(process_metadata, changed_config, paths)

    tampered = deepcopy(run_metadata)
    tampered["runtime"]["python"] = "3.13.0"  # type: ignore[index]
    start_vllm.atomic_write_json(paths["run_metadata"], tampered)
    with pytest.raises(start_vllm.VllmScriptError, match="runtime fingerprint"):
        start_vllm.require_same_week04_run(process_metadata, config, paths)


def test_start_records_group_session_and_week04_identity(
    tmp_path: Path, monkeypatch
) -> None:
    config = make_config()
    paths = {
        **make_paths(tmp_path),
        "log": tmp_path / "server.log",
        "serve_help": tmp_path / "serve-help.txt",
        "vllm_version": tmp_path / "vllm-version.txt",
    }
    run_metadata = create_run_metadata(config, make_source(), make_runtime())
    start_vllm.atomic_write_json(paths["run_metadata"], run_metadata)

    class FakeProcess:
        pid = 41001

        def poll(self):
            return None

        def wait(self, timeout: float):
            return 0

    popen_calls: list[dict[str, object]] = []

    def fake_popen(argv, **kwargs):
        popen_calls.append(kwargs)
        return FakeProcess()

    plan = {
        "argv": ["fake-vllm", "serve"],
        "settings": {
            "model_id": "example/model",
            "served_model_name": "example",
            "host": "127.0.0.1",
            "port": 8000,
        },
        "vllm_version": "0.10.2",
    }
    monkeypatch.setattr(start_vllm, "build_plan", lambda *args, **kwargs: (plan, paths))
    monkeypatch.setattr(start_vllm, "port_is_open", lambda *args: False)
    monkeypatch.setattr(start_vllm.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        start_vllm, "capture_new_session_identity", lambda process: (41001, 41001)
    )
    monkeypatch.setattr(
        start_vllm,
        "capture_process_identity",
        lambda process: ("fake-start", ["fake-vllm", "serve"], 41001, 41001),
    )

    result = start_vllm.start_server(
        tmp_path / "week04.yaml", config, allow_non_loopback=False, no_wait=True
    )

    assert result["status"] == "starting"
    assert len(popen_calls) == 1
    assert popen_calls[0]["start_new_session"] is True
    assert popen_calls[0]["close_fds"] is True
    assert popen_calls[0]["cwd"] == start_vllm.PROJECT_DIR
    stored = start_vllm.read_metadata(paths["pid_metadata"])
    assert stored is not None
    assert (stored["pid"], stored["pgid"], stored["session_id"]) == (
        41001,
        41001,
        41001,
    )
    assert stored["run_id"] == run_metadata["run_id"]
    uuid.UUID(str(stored["server_instance_id"]))
    uuid.UUID(str(stored["server_attempt_id"]))
    assert stored["server_attempt_id"] != stored["server_instance_id"]
    assert stored["config_fingerprint"] == run_metadata["config_fingerprint"]
    assert stored["runtime_fingerprint"] == run_metadata["runtime_fingerprint"]
    assert stored["source"] == artifact_identity(run_metadata)["source"]
    assert stored["model_identity"] == run_metadata["model_identity"]


def test_startup_identity_failure_cleans_group_and_preserves_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    config = make_config()
    paths = {
        **make_paths(tmp_path),
        "log": tmp_path / "server.log",
        "serve_help": tmp_path / "serve-help.txt",
        "vllm_version": tmp_path / "vllm-version.txt",
    }
    run_metadata = create_run_metadata(config, make_source(), make_runtime())
    start_vllm.atomic_write_json(paths["run_metadata"], run_metadata)

    class FakeProcess:
        pid = 41001

        def poll(self):
            return None

    plan = {
        "argv": ["fake-vllm", "serve"],
        "settings": {
            "host": "127.0.0.1",
            "port": 8000,
        },
        "vllm_version": "0.10.2",
    }
    cleanup_calls: list[tuple[int | None, int | None, str, int]] = []
    monkeypatch.setattr(start_vllm, "build_plan", lambda *args, **kwargs: (plan, paths))
    monkeypatch.setattr(start_vllm, "port_is_open", lambda *args: False)
    monkeypatch.setattr(
        start_vllm.subprocess, "Popen", lambda *args, **kwargs: FakeProcess()
    )
    monkeypatch.setattr(
        start_vllm, "capture_new_session_identity", lambda process: (41001, 41001)
    )
    monkeypatch.setattr(start_vllm, "capture_process_identity", lambda process: None)
    monkeypatch.setattr(
        start_vllm,
        "cleanup_unrecorded_process_group",
        lambda process, pgid, session_id, host, port: cleanup_calls.append(
            (pgid, session_id, host, port)
        )
        or None,
    )

    with pytest.raises(
        start_vllm.VllmScriptError, match="identity could not be captured"
    ):
        start_vllm.start_server(
            tmp_path / "week04.yaml",
            config,
            allow_non_loopback=False,
            no_wait=True,
        )

    assert cleanup_calls == [(41001, 41001, "127.0.0.1", 8000)]
    stored = start_vllm.read_metadata(paths["pid_metadata"])
    assert stored is not None
    assert stored["state"] == "startup_failed"
    assert stored["run_id"] == run_metadata["run_id"]
    uuid.UUID(str(stored["server_instance_id"]))
    uuid.UUID(str(stored["server_attempt_id"]))
    assert "identity could not be captured" in stored["startup_error"]


def test_restart_before_readiness_gets_a_new_attempt_id(
    tmp_path: Path, monkeypatch
) -> None:
    config = make_config()
    paths = {
        **make_paths(tmp_path),
        "raw_dir": tmp_path,
        "log": tmp_path / "server.log",
        "serve_help": tmp_path / "serve-help.txt",
        "vllm_version": tmp_path / "vllm-version.txt",
        "models_snapshot": tmp_path / "models.json",
    }
    run_metadata, prior = make_process_metadata(config)
    prior.update(
        {
            "state": "startup_failed",
            "pid": 41000,
            "pgid": 41000,
            "session_id": 41000,
        }
    )
    prior_attempt = str(prior["server_attempt_id"])
    write_metadata(paths, run_metadata, prior)

    class FakeProcess:
        pid = 41001

        def poll(self):
            return None

    plan = {
        "argv": ["fake-vllm", "serve"],
        "settings": {
            "model_id": "example/model",
            "served_model_name": "example",
            "host": "127.0.0.1",
            "port": 8000,
        },
        "vllm_version": "0.10.2",
    }
    monkeypatch.setattr(start_vllm, "build_plan", lambda *args, **kwargs: (plan, paths))
    monkeypatch.setattr(start_vllm, "ownership_status", lambda value: ("not_running", "gone"))
    monkeypatch.setattr(start_vllm, "port_is_open", lambda *args: False)
    monkeypatch.setattr(start_vllm.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(start_vllm, "capture_new_session_identity", lambda process: (41001, 41001))
    monkeypatch.setattr(
        start_vllm,
        "capture_process_identity",
        lambda process: ("new-start", ["fake-vllm", "serve"], 41001, 41001),
    )

    start_vllm.start_server(
        tmp_path / "week04.yaml", config, allow_non_loopback=False, no_wait=True
    )

    stored = start_vllm.read_metadata(paths["pid_metadata"])
    assert stored is not None
    assert stored["server_instance_id"] == prior["server_instance_id"]
    assert stored["server_attempt_id"] != prior_attempt


def test_restart_refuses_completed_server_backed_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    config = make_config()
    config["output"].update(
        {
            "raw_dir": str(tmp_path),
            "nonstream_smoke_json": str(tmp_path / "nonstream.json"),
        }
    )
    paths = {
        **make_paths(tmp_path),
        "raw_dir": tmp_path,
        "log": tmp_path / "server.log",
        "serve_help": tmp_path / "serve-help.txt",
        "vllm_version": tmp_path / "vllm-version.txt",
        "models_snapshot": tmp_path / "models.json",
    }
    run_metadata, prior = make_process_metadata(config)
    prior["state"] = "stopped"
    write_metadata(paths, run_metadata, prior)
    start_vllm.atomic_write_json(tmp_path / "nonstream.json", {"status": "completed"})
    plan = {
        "argv": ["fake-vllm", "serve"],
        "settings": {"host": "127.0.0.1", "port": 8000},
        "vllm_version": "0.10.2",
    }
    monkeypatch.setattr(start_vllm, "build_plan", lambda *args, **kwargs: (plan, paths))
    monkeypatch.setattr(start_vllm, "ownership_status", lambda value: ("not_running", "gone"))
    monkeypatch.setattr(start_vllm, "port_is_open", lambda *args: False)
    monkeypatch.setattr(
        start_vllm.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("must not start after completed evidence"),
    )

    with pytest.raises(start_vllm.VllmScriptError, match="completed server-backed evidence"):
        start_vllm.start_server(
            tmp_path / "week04.yaml", config, allow_non_loopback=False, no_wait=True
        )


def test_cleanup_unrecorded_process_group_signals_verified_group(
    monkeypatch,
) -> None:
    class FakeProcess:
        pid = 41001

        def poll(self):
            return None

    signals: list[tuple[signal.Signals, bool]] = []
    monkeypatch.setattr(start_vllm, "current_process_group", lambda: 999)
    monkeypatch.setattr(start_vllm, "group_exists", lambda pgid: True)
    monkeypatch.setattr(
        start_vllm,
        "signal_owned_process_group",
        lambda metadata, signum, *, require_leader: signals.append(
            (signum, require_leader)
        ),
    )
    monkeypatch.setattr(
        start_vllm,
        "wait_for_group_and_port_closed",
        lambda *args: (False, False, True),
    )

    detail = start_vllm.cleanup_unrecorded_process_group(
        FakeProcess(), 41001, 41001, "127.0.0.1", 8000
    )

    assert detail is None
    assert signals == [(signal.SIGTERM, False)]


def test_cleanup_unrecorded_process_group_refuses_controller_group(
    monkeypatch,
) -> None:
    class FakeProcess:
        pid = 41001

        def poll(self):
            return None

    monkeypatch.setattr(start_vllm, "current_process_group", lambda: 41001)
    monkeypatch.setattr(
        start_vllm.os,
        "killpg",
        lambda *_: pytest.fail("must never signal the controller group"),
    )

    detail = start_vllm.cleanup_unrecorded_process_group(
        FakeProcess(), 41001, 41001, "127.0.0.1", 8000
    )

    assert detail is not None and "own process group" in detail
