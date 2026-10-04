from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.vllm_contract import (
    PINNED_VLLM_VERSION,
    is_loopback_host,
    server_argv,
    validate_config,
    validate_help_support,
)
from src.week04_contract import (
    artifact_identity,
    load_run_metadata,
    validate_artifact_identity,
)

EXPECTED_VLLM_VERSION = PINNED_VLLM_VERSION
GPU_COMPATIBILITY = {
    "status": "unverified",
    "reason": (
        "GPU compatibility remains unverified until a generation smoke succeeds on "
        "the target GPU; CLI, /health, and /v1/models checks are not sufficient."
    ),
}


class VllmScriptError(RuntimeError):
    """An actionable, user-facing lifecycle error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def load_config(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as error:
        raise VllmScriptError(
            "PyYAML is required to read the Week 4 config. Run the vLLM bootstrap first."
        ) from error

    if not path.is_file():
        raise VllmScriptError(f"Config file does not exist: {path}")
    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise VllmScriptError(f"Config must contain a YAML mapping: {path}")
    return payload


def resolve_config_path(raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        cwd_candidate = (Path.cwd() / candidate).resolve()
        candidate = (
            cwd_candidate
            if cwd_candidate.exists()
            else (PROJECT_DIR / candidate).resolve()
        )
    return candidate


def resolve_artifact_path(raw_path: object, default: str) -> Path:
    candidate = Path(str(raw_path or default)).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_DIR / candidate
    return candidate.resolve()


def require_mapping(value: object, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise VllmScriptError(f"{name} must be a mapping")
    return value


def positive_float(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise VllmScriptError(
            f"{name} must be a positive number, got {value!r}"
        ) from error
    if result <= 0:
        raise VllmScriptError(f"{name} must be positive, got {result}")
    return result


def positive_int(value: object, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise VllmScriptError(
            f"{name} must be a positive integer, got {value!r}"
        ) from error
    if result <= 0:
        raise VllmScriptError(f"{name} must be positive, got {result}")
    return result


def configured_paths(config: Mapping[str, Any]) -> dict[str, Path]:
    output = require_mapping(config.get("output"), "output")
    server = require_mapping(config.get("server"), "server")
    raw_dir = resolve_artifact_path(output.get("raw_dir"), "results/week04/raw")

    def under_raw(value: object, filename: str) -> Path:
        if value:
            return resolve_artifact_path(value, str(raw_dir / filename))
        return (raw_dir / filename).resolve()

    return {
        "raw_dir": raw_dir,
        "vllm_version": under_raw(output.get("vllm_version"), "vllm-version.txt"),
        "vllm_help": under_raw(output.get("vllm_help"), "vllm-help.txt"),
        "serve_help": under_raw(
            server.get("serve_help_path") or output.get("serve_help"),
            "serve-help.txt",
        ),
        "log": under_raw(server.get("log_path"), "vllm-server.log"),
        "pid_metadata": under_raw(
            server.get("pid_metadata_path"), "vllm-server.pid.json"
        ),
        "models_snapshot": under_raw(
            server.get("models_snapshot_path"), "vllm-models.json"
        ),
        "run_metadata": resolve_artifact_path(
            output.get("run_metadata"), "results/week04/raw/run-metadata.json"
        ),
    }


def load_week04_run_identity(
    config: Mapping[str, Any], paths: Mapping[str, Path]
) -> tuple[dict[str, object], dict[str, object]]:
    """Load the authoritative Week 4 run and return its artifact identity."""

    try:
        metadata = load_run_metadata(config, paths["run_metadata"])
        identity = artifact_identity(metadata)
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise VllmScriptError(
            "Cannot bind the vLLM server to valid Week 4 run metadata at "
            f"{paths['run_metadata']}: {error}"
        ) from error
    return metadata, identity


def require_same_week04_run(
    server_metadata: Mapping[str, Any],
    config: Mapping[str, Any],
    paths: Mapping[str, Path],
) -> dict[str, object]:
    """Reject lifecycle actions against a different experiment identity."""

    run_metadata, _ = load_week04_run_identity(config, paths)
    try:
        validate_artifact_identity(
            server_metadata, run_metadata, "vLLM server process metadata"
        )
    except (KeyError, TypeError, ValueError) as error:
        raise VllmScriptError(
            "Refusing lifecycle action because the server is not bound to the "
            f"current Week 4 run: {error}"
        ) from error
    return run_metadata


def find_vllm_binary() -> Path:
    override = os.environ.get("VLLM_BIN")
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        return candidate
    dedicated = PROJECT_DIR / ".venv-vllm" / "bin" / "vllm"
    if dedicated.is_file():
        return dedicated.resolve()
    discovered = shutil.which("vllm")
    if discovered:
        return Path(discovered).resolve()
    return dedicated.resolve()


def run_and_capture(argv: Sequence[str], destination: Path) -> str:
    try:
        completed = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except OSError as error:
        atomic_write_text(destination, f"unable to execute {argv[0]}: {error}\n")
        raise VllmScriptError(f"Unable to execute {argv[0]}: {error}") from error
    atomic_write_text(destination, completed.stdout)
    if completed.returncode != 0:
        raise VllmScriptError(
            f"Command exited {completed.returncode}; preserved output at {destination}: "
            f"{list(argv)!r}"
        )
    return completed.stdout


def query_installed_version(vllm_binary: Path, destination: Path) -> str:
    python_override = os.environ.get("VLLM_PYTHON")
    sibling_python = vllm_binary.parent / "python"
    if python_override:
        version_argv = [
            python_override,
            "-c",
            "from importlib.metadata import version; print(version('vllm'))",
        ]
    elif sibling_python.is_file():
        version_argv = [
            str(sibling_python),
            "-c",
            "from importlib.metadata import version; print(version('vllm'))",
        ]
    else:
        version_argv = [str(vllm_binary), "--version"]

    output = run_and_capture(version_argv, destination)
    matches = re.findall(r"(?<![0-9.])([0-9]+\.[0-9]+\.[0-9]+)(?![0-9.])", output)
    version = matches[-1] if matches else output.strip()
    if version != EXPECTED_VLLM_VERSION:
        raise VllmScriptError(
            f"Expected vllm=={EXPECTED_VLLM_VERSION}, got {version!r}; "
            f"version evidence: {destination}"
        )
    return version


def capture_cli_evidence(
    vllm_binary: Path, paths: Mapping[str, Path]
) -> tuple[str, str]:
    if not vllm_binary.is_file():
        raise VllmScriptError(
            f"Dedicated vLLM executable not found: {vllm_binary}. "
            "Run scripts/bootstrap_vllm_gcp.sh first."
        )
    version = query_installed_version(vllm_binary, paths["vllm_version"])
    run_and_capture([str(vllm_binary), "--help"], paths["vllm_help"])
    serve_help = run_and_capture(
        [str(vllm_binary), "serve", "--help"], paths["serve_help"]
    )
    return version, serve_help


def build_server_argv(
    config: Mapping[str, Any], vllm_binary: Path, allow_non_loopback: bool
) -> tuple[list[str], dict[str, Any]]:
    try:
        validated = validate_config(config)
    except ValueError as error:
        raise VllmScriptError(f"Invalid Week 4 config: {error}") from error
    model = require_mapping(validated.get("model"), "model")
    server = require_mapping(validated.get("server"), "server")
    model_id = str(model["id"])
    host = str(server["host"])
    if not is_loopback_host(host) and not allow_non_loopback:
        raise VllmScriptError(
            f"Refusing non-loopback host {host!r}. Use --allow-non-loopback only when "
            "the endpoint is protected; localhost or SSH forwarding is the baseline."
        )
    port = int(server["port"])
    argv = server_argv(validated, executable=str(vllm_binary))
    settings = {
        "model_id": model_id,
        "served_model_name": str(model["served_model_name"]),
        "host": host,
        "port": port,
        "startup_timeout_seconds": positive_float(
            server.get("startup_timeout_seconds", 900),
            "server.startup_timeout_seconds",
        ),
        "readiness_poll_seconds": positive_float(
            server.get("readiness_poll_seconds", 2),
            "server.readiness_poll_seconds",
        ),
        "shutdown_timeout_seconds": positive_float(
            server.get("shutdown_timeout_seconds", 30),
            "server.shutdown_timeout_seconds",
        ),
        "allow_non_loopback": allow_non_loopback,
    }
    return argv, settings


def readiness_host(host: str) -> str:
    normalized = host.strip().lower().strip("[]")
    if normalized in {"0.0.0.0", "::"}:
        return "127.0.0.1" if normalized == "0.0.0.0" else "::1"
    return normalized


def endpoint_url(host: str, port: int, path: str) -> str:
    connect_host = readiness_host(host)
    if ":" in connect_host:
        connect_host = f"[{connect_host}]"
    return f"http://{connect_host}:{port}{path}"


def process_snapshot_procfs(pid: int) -> tuple[str, list[str]] | None:
    proc_dir = Path("/proc") / str(pid)
    stat_path = proc_dir / "stat"
    cmdline_path = proc_dir / "cmdline"
    if not stat_path.is_file() or not cmdline_path.is_file():
        return None
    try:
        stat_text = stat_path.read_text(encoding="utf-8")
        closing_paren = stat_text.rfind(")")
        fields_after_command = stat_text[closing_paren + 2 :].split()
        if closing_paren < 0 or len(fields_after_command) < 20:
            return None
        if fields_after_command[0] == "Z":
            return None
        start_ticks = fields_after_command[19]
        boot_id_path = Path("/proc/sys/kernel/random/boot_id")
        boot_id = (
            boot_id_path.read_text(encoding="utf-8").strip()
            if boot_id_path.is_file()
            else "unknown-boot"
        )
        raw_argv = cmdline_path.read_bytes()
        argv = [
            part.decode(errors="surrogateescape")
            for part in raw_argv.split(b"\0")
            if part
        ]
    except (OSError, UnicodeError):
        return None
    if not argv:
        return None
    return f"linux-procfs:{boot_id}:{start_ticks}", argv


def process_snapshot_psutil(pid: int) -> tuple[str, list[str]] | None:
    try:
        import psutil

        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        identity = f"psutil:{process.create_time():.6f}"
        argv = process.cmdline()
    except ImportError:
        return None
    except Exception as error:  # psutil has platform-specific exception subclasses.
        if error.__class__.__module__.startswith("psutil"):
            return None
        raise
    if not argv:
        return None
    return identity, argv


def process_snapshot_ps(pid: int) -> tuple[str, list[str]] | None:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        return None
    line = completed.stdout.strip()
    if completed.returncode != 0 or not line:
        return None
    match = re.match(
        r"^([A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+(.+)$",
        line,
    )
    if match is None:
        return None
    # This fallback is intentionally conservative. It is sufficient for launch
    # ownership on POSIX systems without procfs/psutil, and refusal is safer than
    # guessing if the command contains shell quoting that cannot round-trip.
    try:
        import shlex

        argv = shlex.split(match.group(2))
    except ValueError:
        return None
    return (f"posix-ps:{match.group(1)}", argv) if argv else None


def darwin_process_argv(pid: int) -> list[str] | None:
    if sys.platform != "darwin":
        return None
    library_name = ctypes.util.find_library("c")
    if not library_name:
        return None
    libc = ctypes.CDLL(library_name, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid
    size = ctypes.c_size_t()
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value == 0:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        return None
    data = bytes(buffer.raw[: size.value])
    if len(data) < ctypes.sizeof(ctypes.c_int):
        return None
    argc = ctypes.c_int.from_buffer_copy(data[: ctypes.sizeof(ctypes.c_int)]).value
    payload = data[ctypes.sizeof(ctypes.c_int) :]
    executable_end = payload.find(b"\0")
    if argc <= 0 or executable_end < 0:
        return None
    cursor = executable_end
    while cursor < len(payload) and payload[cursor] == 0:
        cursor += 1
    argv: list[str] = []
    for _ in range(argc):
        end = payload.find(b"\0", cursor)
        if end < 0:
            return None
        argv.append(payload[cursor:end].decode(errors="surrogateescape"))
        cursor = end + 1
    return argv or None


def process_snapshot_darwin(pid: int) -> tuple[str, list[str]] | None:
    argv = darwin_process_argv(pid)
    if argv is None:
        return None
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        return None
    started = completed.stdout.strip()
    if completed.returncode != 0 or not started:
        return None
    return f"posix-ps:{started}", argv


def process_snapshot(pid: int) -> tuple[str, list[str]] | None:
    if pid <= 0:
        return None
    snapshot = process_snapshot_procfs(pid)
    if snapshot is not None:
        return snapshot
    snapshot = process_snapshot_psutil(pid)
    if snapshot is not None:
        return snapshot
    snapshot = process_snapshot_darwin(pid)
    if snapshot is not None:
        return snapshot
    return process_snapshot_ps(pid)


def process_group_id(pid: int) -> int | None:
    if pid <= 0:
        return None
    try:
        return os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return None


def process_session_id(pid: int) -> int | None:
    if pid <= 0:
        return None
    try:
        return os.getsid(pid)
    except (OSError, ProcessLookupError):
        return None


def group_exists(pgid: int) -> bool:
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def current_process_group() -> int | None:
    try:
        return os.getpgrp()
    except OSError:
        return None


def validate_owned_process_group(
    metadata: Mapping[str, Any], *, require_leader: bool
) -> tuple[int, int, int]:
    """Validate immutable leader/session facts before signaling a group."""

    try:
        pid = int(metadata["pid"])
        pgid = int(metadata["pgid"])
        session_id = int(metadata["session_id"])
    except (KeyError, TypeError, ValueError) as error:
        raise VllmScriptError(
            "Process metadata lacks a valid pid, pgid, or session_id"
        ) from error
    if pid <= 0 or pgid <= 0 or session_id <= 0:
        raise VllmScriptError(
            f"Invalid process identity pid={pid}, pgid={pgid}, session_id={session_id}"
        )
    if pgid != pid or session_id != pid:
        raise VllmScriptError(
            "Refusing to signal a process group that was not created as an owned "
            f"start_new_session leader: pid={pid}, pgid={pgid}, session_id={session_id}"
        )
    own_pgid = current_process_group()
    if own_pgid is not None and pgid == own_pgid:
        raise VllmScriptError(f"Refusing to signal our own process group {pgid}")
    if require_leader:
        state, reason = ownership_status(metadata)
        if state != "owned_running":
            raise VllmScriptError(
                f"Process-group leader is not safely owned ({state}: {reason})"
            )
        actual_pgid = process_group_id(pid)
        actual_session_id = process_session_id(pid)
        if actual_pgid != pgid or actual_session_id != session_id:
            raise VllmScriptError(
                "Live process group/session identity differs from ownership metadata: "
                f"expected pgid={pgid}, session_id={session_id}; "
                f"observed pgid={actual_pgid}, session_id={actual_session_id}"
            )
    return pid, pgid, session_id


def signal_owned_process_group(
    metadata: Mapping[str, Any], signum: signal.Signals, *, require_leader: bool
) -> None:
    _, pgid, _ = validate_owned_process_group(metadata, require_leader=require_leader)
    if not group_exists(pgid):
        return
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        return


def capture_process_identity(
    process: subprocess.Popen[bytes], timeout_seconds: float = 2.0
) -> tuple[str, list[str], int, int] | None:
    deadline = time.monotonic() + timeout_seconds
    previous: tuple[str, list[str], int, int] | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return None
        snapshot = process_snapshot(process.pid)
        pgid = process_group_id(process.pid)
        session_id = process_session_id(process.pid)
        current = (
            (snapshot[0], snapshot[1], pgid, session_id)
            if snapshot is not None and pgid is not None and session_id is not None
            else None
        )
        if current is not None and current == previous:
            return current
        previous = current
        time.sleep(0.05)
    return previous


def capture_new_session_identity(
    process: subprocess.Popen[bytes],
) -> tuple[int | None, int | None]:
    """Capture PGID/SID as soon as Popen returns from start_new_session."""

    if process.poll() is not None:
        return None, None
    return process_group_id(process.pid), process_session_id(process.pid)


def read_metadata(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VllmScriptError(
            f"Cannot read PID ownership metadata {path}: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise VllmScriptError(f"PID ownership metadata is not a JSON object: {path}")
    return payload


def ownership_status(metadata: Mapping[str, Any]) -> tuple[str, str]:
    try:
        pid = int(metadata["pid"])
    except (KeyError, TypeError, ValueError):
        return "unverifiable", "metadata has no valid pid"
    snapshot = process_snapshot(pid)
    if snapshot is None:
        return "not_running", f"pid {pid} is not running"
    actual_identity, actual_argv = snapshot
    expected_identity = metadata.get("start_identity")
    expected_argv = metadata.get("process_argv")
    if not isinstance(expected_identity, str) or not isinstance(expected_argv, list):
        return "unverifiable", "metadata lacks start_identity or process_argv"
    if actual_identity != expected_identity:
        return "not_owned", "process start identity differs from ownership metadata"
    if actual_argv != expected_argv:
        return "not_owned", "process argv differs from ownership metadata"
    return "owned_running", "pid, start identity, and exact observed argv match"


@contextmanager
def lifecycle_lock(metadata_path: Path) -> Iterator[None]:
    lock_path = metadata_path.with_name(f"{metadata_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((readiness_host(host), port), timeout=0.5):
            return True
    except OSError:
        return False


def configured_endpoint(config: Mapping[str, Any]) -> tuple[str, int]:
    server = require_mapping(config.get("server"), "server")
    host = str(server.get("host"))
    port = positive_int(server.get("port"), "server.port")
    return host, port


def wait_for_group_and_port_closed(
    pgid: int, host: str, port: int, timeout_seconds: float
) -> tuple[bool, bool, bool]:
    """Return group-alive, port-open, and whether the group vanished while waiting."""

    deadline = time.monotonic() + timeout_seconds
    group_disappeared = False
    while True:
        group_alive = group_exists(pgid)
        port_open = port_is_open(host, port)
        group_disappeared = group_disappeared or not group_alive
        if not group_alive and not port_open:
            return False, False, group_disappeared
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return group_alive, port_open, group_disappeared
        time.sleep(min(0.1, remaining))


def shutdown_detail(pgid: int, host: str, port: int) -> str:
    group_alive = group_exists(pgid)
    port_open = port_is_open(host, port)
    return (
        f"process_group_{pgid}={'alive' if group_alive else 'gone'}, "
        f"configured_port_{host}:{port}={'open' if port_open else 'closed'}"
    )


def cleanup_unrecorded_process_group(
    process: subprocess.Popen[bytes],
    pgid: int | None,
    session_id: int | None,
    host: str,
    port: int,
) -> str | None:
    """Best-effort cleanup before durable ownership metadata can be written."""

    provisional = {
        "pid": process.pid,
        "pgid": pgid,
        "session_id": session_id,
    }
    try:
        _, owned_pgid, _ = validate_owned_process_group(
            provisional, require_leader=False
        )
    except VllmScriptError as error:
        return f"group cleanup refused safely: {error}"
    if not group_exists(owned_pgid):
        if port_is_open(host, port):
            return (
                "verified startup group is gone, but the configured port remains "
                f"open: {host}:{port}"
            )
        return None
    signal_owned_process_group(provisional, signal.SIGTERM, require_leader=False)
    group_alive, port_open, group_disappeared = wait_for_group_and_port_closed(
        owned_pgid, host, port, 5.0
    )
    if not group_alive and not port_open:
        return None
    if group_disappeared:
        return (
            "verified startup group disappeared, but the configured port remains "
            f"open: {host}:{port}"
        )
    signal_owned_process_group(provisional, signal.SIGKILL, require_leader=False)
    group_alive, port_open, _ = wait_for_group_and_port_closed(
        owned_pgid, host, port, 5.0
    )
    if group_alive or port_open:
        return f"startup cleanup incomplete: {shutdown_detail(owned_pgid, host, port)}"
    return None


def record_startup_failure(
    metadata_path: Path,
    *,
    process: subprocess.Popen[bytes],
    server_instance_id: str,
    server_attempt_id: str,
    pgid: int | None,
    session_id: int | None,
    config_path: Path,
    argv: Sequence[str],
    settings: Mapping[str, Any],
    paths: Mapping[str, Path],
    run_identity: Mapping[str, object],
    failure: str,
) -> None:
    atomic_write_json(
        metadata_path,
        {
            "schema_version": 1,
            "server_instance_id": server_instance_id,
            "server_attempt_id": server_attempt_id,
            "state": "startup_failed",
            "pid": process.pid,
            "pgid": pgid,
            "session_id": session_id,
            "leader_returncode": process.poll(),
            "argv": list(argv),
            "started_at": utc_now(),
            "startup_failed_at": utc_now(),
            "startup_error": failure,
            "config_path": str(config_path),
            "settings": dict(settings),
            "log_path": str(paths["log"]),
            "gpu_compatibility": GPU_COMPATIBILITY,
            **run_identity,
        },
    )


def completed_server_evidence(
    config: Mapping[str, Any], paths: Mapping[str, Path]
) -> list[Path]:
    """Return published evidence that makes a same-run restart unsafe.

    A new process attempt may replace failed pre-readiness ownership metadata, but
    it must never share a logical run with evidence produced by an older ready
    process.  Partial files and incomplete markers are intentionally excluded.
    """

    output = require_mapping(config.get("output"), "output")
    raw_dir = paths.get("raw_dir", paths["pid_metadata"].parent)

    def output_path(key: str, filename: str) -> Path:
        value = output.get(key)
        return (
            resolve_artifact_path(value, str(raw_dir / filename))
            if value
            else raw_dir / filename
        )

    completed: list[Path] = []
    readiness = paths.get("models_snapshot", raw_dir / "vllm-models.json")
    if readiness.is_file() and readiness.stat().st_size > 0:
        completed.append(readiness)

    for key, filename in (
        ("nonstream_smoke_json", "openai-nonstream-smoke.json"),
        ("stream_smoke_json", "openai-stream-smoke.json"),
    ):
        candidate = output_path(key, filename)
        if not candidate.is_file():
            continue
        try:
            payload = read_metadata(candidate)
        except VllmScriptError:
            payload = None
        if payload is not None and payload.get("status") == "completed":
            completed.append(candidate)

    for directory_name in ("benchmark-smoke", "benchmark"):
        for candidate in sorted((raw_dir / directory_name).glob("*.complete.json")):
            try:
                payload = read_metadata(candidate)
            except VllmScriptError:
                payload = None
            if payload is not None and payload.get("status") == "completed":
                completed.append(candidate)

    replay_dir_value = output.get("trace_replay_dir")
    replay_dir = (
        resolve_artifact_path(replay_dir_value, str(raw_dir / "trace-replay"))
        if replay_dir_value
        else raw_dir / "trace-replay"
    )
    completed.extend(
        candidate
        for candidate in sorted(replay_dir.glob("*.json"))
        if candidate.is_file() and candidate.stat().st_size > 0
    )
    comparison = output_path("comparison_manifest", "comparison-manifest.json")
    if comparison.is_file() and comparison.stat().st_size > 0:
        completed.append(comparison)
    return completed


def request_endpoint(url: str, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=max(0.1, timeout)) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def require_owned_running(metadata: Mapping[str, Any], metadata_path: Path) -> None:
    state, reason = ownership_status(metadata)
    if state != "owned_running":
        raise VllmScriptError(
            f"Server process is not safely owned ({state}: {reason}); metadata: {metadata_path}"
        )
    validate_owned_process_group(metadata, require_leader=True)


def wait_until_ready(
    config: Mapping[str, Any],
    paths: Mapping[str, Path],
    timeout_override: float | None = None,
) -> dict[str, Any]:
    metadata = read_metadata(paths["pid_metadata"])
    if metadata is None:
        raise VllmScriptError(f"No server PID metadata exists: {paths['pid_metadata']}")
    require_same_week04_run(metadata, config, paths)
    require_owned_running(metadata, paths["pid_metadata"])
    settings = require_mapping(metadata.get("settings"), "metadata.settings")
    host = str(settings.get("host"))
    port = positive_int(settings.get("port"), "metadata.settings.port")
    timeout = (
        positive_float(timeout_override, "--timeout-seconds")
        if timeout_override is not None
        else positive_float(
            settings.get("startup_timeout_seconds", 900), "startup timeout"
        )
    )
    poll_seconds = positive_float(
        settings.get("readiness_poll_seconds", 2), "readiness poll interval"
    )
    health_url = endpoint_url(host, port, "/health")
    models_url = endpoint_url(host, port, "/v1/models")
    deadline = time.monotonic() + timeout
    last_error = "no response"

    while time.monotonic() < deadline:
        require_owned_running(metadata, paths["pid_metadata"])
        remaining = deadline - time.monotonic()
        try:
            status, _ = request_endpoint(health_url, min(2.0, remaining))
            if 200 <= status < 300:
                break
            last_error = f"HTTP {status}"
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            last_error = str(error)
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
    else:
        raise VllmScriptError(
            f"Timed out after {timeout:g}s waiting for {health_url}: {last_error}"
        )

    expected_models = {
        str(settings.get("model_id")),
        str(settings.get("served_model_name")),
    }
    models_payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        require_owned_running(metadata, paths["pid_metadata"])
        remaining = deadline - time.monotonic()
        try:
            status, body = request_endpoint(models_url, min(2.0, remaining))
            if 200 <= status < 300:
                parsed = json.loads(body)
                if not isinstance(parsed, dict) or not isinstance(
                    parsed.get("data"), list
                ):
                    last_error = "response is not an OpenAI model-list object"
                else:
                    advertised = {
                        str(item.get("id"))
                        for item in parsed["data"]
                        if isinstance(item, dict) and item.get("id") is not None
                    }
                    if advertised.intersection(expected_models):
                        models_payload = parsed
                        break
                    last_error = (
                        f"expected one of {sorted(expected_models)!r}, advertised "
                        f"{sorted(advertised)!r}"
                    )
            else:
                last_error = f"HTTP {status}"
        except (
            OSError,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
        ) as error:
            last_error = str(error)
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
    if models_payload is None:
        raise VllmScriptError(
            f"Timed out after {timeout:g}s waiting for {models_url}: {last_error}"
        )

    ready_at = utc_now()
    server_instance_id = str(metadata.get("server_instance_id", ""))
    server_attempt_id = str(metadata.get("server_attempt_id", ""))
    try:
        if str(uuid.UUID(server_instance_id)) != server_instance_id:
            raise ValueError
        if str(uuid.UUID(server_attempt_id)) != server_attempt_id:
            raise ValueError
    except ValueError as error:
        raise VllmScriptError(
            "Server ownership metadata lacks canonical instance/attempt UUIDs"
        ) from error
    atomic_write_json(
        paths["models_snapshot"],
        {
            "schema_version": 1,
            "status": "ready",
            "captured_at": ready_at,
            **artifact_identity(require_same_week04_run(metadata, config, paths)),
            "server_instance_id": server_instance_id,
            "server_attempt_id": server_attempt_id,
            "health_url": health_url,
            "models_url": models_url,
            "response": models_payload,
        },
    )
    with lifecycle_lock(paths["pid_metadata"]):
        current = read_metadata(paths["pid_metadata"])
        if current is None or current.get("pid") != metadata.get("pid"):
            raise VllmScriptError("PID metadata changed while waiting for readiness")
        require_same_week04_run(current, config, paths)
        require_owned_running(current, paths["pid_metadata"])
        current.update(
            {
                "state": "ready",
                "ready_at": ready_at,
                "health_url": health_url,
                "models_url": models_url,
                "models_snapshot_path": str(paths["models_snapshot"]),
                "gpu_compatibility": GPU_COMPATIBILITY,
            }
        )
        atomic_write_json(paths["pid_metadata"], current)
    return {
        "status": "ready",
        "pid": metadata["pid"],
        "health_url": health_url,
        "models_url": models_url,
        "models_snapshot_path": str(paths["models_snapshot"]),
        "gpu_compatibility": GPU_COMPATIBILITY,
    }


def build_plan(
    config_path: Path,
    config: Mapping[str, Any],
    allow_non_loopback: bool,
    require_capability_preflight: bool = False,
) -> tuple[dict[str, Any], dict[str, Path]]:
    paths = configured_paths(config)
    vllm_binary = find_vllm_binary()
    argv, settings = build_server_argv(config, vllm_binary, allow_non_loopback)
    capability_preflight = "pending"
    version: str | None = None
    if require_capability_preflight:
        version, serve_help = capture_cli_evidence(vllm_binary, paths)
        try:
            validate_help_support(argv[2:], serve_help)
        except ValueError as error:
            raise VllmScriptError(
                f"{error}; captured authority: {paths['serve_help']}"
            ) from error
        capability_preflight = "passed"
    plan = {
        "schema_version": 1,
        "action": "vllm-serve",
        "config_path": str(config_path),
        "vllm_version": version,
        "expected_vllm_version": EXPECTED_VLLM_VERSION,
        "capability_preflight": capability_preflight,
        "argv": argv,
        "settings": settings,
        "artifacts": {name: str(path) for name, path in paths.items()},
        "gpu_compatibility": GPU_COMPATIBILITY,
    }
    return plan, paths


def start_server(
    config_path: Path,
    config: Mapping[str, Any],
    allow_non_loopback: bool,
    no_wait: bool,
) -> dict[str, Any]:
    plan, paths = build_plan(
        config_path, config, allow_non_loopback, require_capability_preflight=True
    )
    argv = list(plan["argv"])
    settings = dict(plan["settings"])
    metadata_path = paths["pid_metadata"]
    _, run_identity = load_week04_run_identity(config, paths)
    server_instance_id = str(uuid.uuid4())

    with lifecycle_lock(metadata_path):
        existing = read_metadata(metadata_path)
        if existing is not None:
            state, reason = ownership_status(existing)
            if state == "owned_running":
                try:
                    require_same_week04_run(existing, config, paths)
                    validate_owned_process_group(existing, require_leader=True)
                except VllmScriptError as error:
                    raise VllmScriptError(
                        "A live process matches stale server metadata but is not owned by "
                        f"this Week 4 run; refusing to replace or signal it: {error}"
                    ) from error
                raise VllmScriptError(
                    f"An owned vLLM server is already running as pid {existing.get('pid')}"
                )
            try:
                require_same_week04_run(existing, config, paths)
            except VllmScriptError as error:
                raise VllmScriptError(
                    "Refusing to replace server metadata from a different Week 4 run; "
                    "archive the prior results before creating a new run"
                ) from error
            existing_instance = existing.get("server_instance_id")
            if existing_instance is not None:
                try:
                    uuid.UUID(str(existing_instance))
                except ValueError as error:
                    raise VllmScriptError(
                        "Existing server metadata has an invalid logical instance UUID"
                    ) from error
                server_instance_id = str(existing_instance)
            if state in {"not_owned", "unverifiable"} and port_is_open(
                str(settings["host"]), int(settings["port"])
            ):
                raise VllmScriptError(
                    f"Refusing to replace unsafe PID metadata while the configured port is "
                    f"occupied ({state}: {reason}): {metadata_path}"
                )
        if port_is_open(str(settings["host"]), int(settings["port"])):
            raise VllmScriptError(
                f"Refusing to start: {settings['host']}:{settings['port']} is already accepting "
                "connections and is not the owned process in the PID metadata"
            )
        completed = completed_server_evidence(config, paths)
        if completed:
            rendered = ", ".join(str(path) for path in completed[:5])
            if len(completed) > 5:
                rendered += f", ... ({len(completed)} total)"
            raise VllmScriptError(
                "Refusing to start a new server attempt because this logical run "
                f"already has completed server-backed evidence: {rendered}. "
                "Archive the run and initialize a new run UUID."
            )

        paths["log"].parent.mkdir(parents=True, exist_ok=True)
        server_attempt_id = str(uuid.uuid4())
        with paths["log"].open("ab", buffering=0) as log_handle:
            header = (
                f"\n=== vLLM start {utc_now()} ===\n"
                f"server_attempt_id={server_attempt_id}\n"
                f"argv={json.dumps(argv)}\n"
                f"gpu_compatibility={json.dumps(GPU_COMPATIBILITY, sort_keys=True)}\n"
            ).encode()
            log_handle.write(header)
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    cwd=PROJECT_DIR,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as error:
                raise VllmScriptError(f"Unable to start vLLM: {error}") from error

        initial_pgid, initial_session_id = capture_new_session_identity(process)
        snapshot = capture_process_identity(process)
        if snapshot is None:
            cleanup_error = cleanup_unrecorded_process_group(
                process,
                initial_pgid,
                initial_session_id,
                str(settings["host"]),
                int(settings["port"]),
            )
            failure = (
                "vLLM exited early or its process-group identity could not be captured; "
                f"log: {paths['log']}"
                + (f"; cleanup detail: {cleanup_error}" if cleanup_error else "")
            )
            record_startup_failure(
                metadata_path,
                process=process,
                server_instance_id=server_instance_id,
                server_attempt_id=server_attempt_id,
                pgid=initial_pgid,
                session_id=initial_session_id,
                config_path=config_path,
                argv=argv,
                settings=settings,
                paths=paths,
                run_identity=run_identity,
                failure=failure,
            )
            raise VllmScriptError(failure)
        start_identity, observed_argv, pgid, session_id = snapshot
        if (initial_pgid, initial_session_id) != (pgid, session_id):
            cleanup_error = cleanup_unrecorded_process_group(
                process,
                initial_pgid,
                initial_session_id,
                str(settings["host"]),
                int(settings["port"]),
            )
            failure = (
                "vLLM process-group/session identity changed during startup: "
                f"initial=({initial_pgid}, {initial_session_id}), "
                f"stable=({pgid}, {session_id}); log: {paths['log']}"
                + (f"; cleanup detail: {cleanup_error}" if cleanup_error else "")
            )
            record_startup_failure(
                metadata_path,
                process=process,
                server_instance_id=server_instance_id,
                server_attempt_id=server_attempt_id,
                pgid=initial_pgid,
                session_id=initial_session_id,
                config_path=config_path,
                argv=argv,
                settings=settings,
                paths=paths,
                run_identity=run_identity,
                failure=failure,
            )
            raise VllmScriptError(failure)
        if pgid != process.pid or session_id != process.pid:
            cleanup_error = cleanup_unrecorded_process_group(
                process,
                pgid,
                session_id,
                str(settings["host"]),
                int(settings["port"]),
            )
            failure = (
                "vLLM did not honor start_new_session ownership: "
                f"pid={process.pid}, pgid={pgid}, session_id={session_id}; log: {paths['log']}"
                + (
                    f"; cleanup refused safely: {cleanup_error}"
                    if cleanup_error
                    else ""
                )
            )
            record_startup_failure(
                metadata_path,
                process=process,
                server_instance_id=server_instance_id,
                server_attempt_id=server_attempt_id,
                pgid=pgid,
                session_id=session_id,
                config_path=config_path,
                argv=argv,
                settings=settings,
                paths=paths,
                run_identity=run_identity,
                failure=failure,
            )
            raise VllmScriptError(failure)
        metadata = {
            "schema_version": 1,
            # Stable across validated restarts in one run. Each actual process
            # start receives a separate attempt UUID below.
            "server_instance_id": server_instance_id,
            "server_attempt_id": server_attempt_id,
            "state": "starting",
            "pid": process.pid,
            "pgid": pgid,
            "session_id": session_id,
            "start_identity": start_identity,
            "argv": argv,
            "process_argv": observed_argv,
            "started_at": utc_now(),
            "config_path": str(config_path),
            "settings": settings,
            "log_path": str(paths["log"]),
            "serve_help_path": str(paths["serve_help"]),
            "vllm_version_path": str(paths["vllm_version"]),
            "vllm_version": plan["vllm_version"],
            "gpu_compatibility": GPU_COMPATIBILITY,
            **run_identity,
        }
        atomic_write_json(metadata_path, metadata)

    if no_wait:
        return {
            "status": "starting",
            "pid": metadata["pid"],
            "pid_metadata_path": str(metadata_path),
            "log_path": str(paths["log"]),
            "gpu_compatibility": GPU_COMPATIBILITY,
        }
    try:
        return wait_until_ready(config, paths)
    except BaseException as error:
        with lifecycle_lock(metadata_path):
            current = read_metadata(metadata_path)
            if current is not None and current.get("pid") == metadata.get("pid"):
                current["state"] = "readiness_failed"
                current["readiness_failed_at"] = utc_now()
                current["readiness_error"] = str(error)
                atomic_write_json(metadata_path, current)
        try:
            stop_server(config, paths, timeout_override=5.0, failure_context=str(error))
        except VllmScriptError as stop_error:
            raise VllmScriptError(
                f"{error}; automatic cleanup also failed safely: {stop_error}"
            ) from error
        raise


def stop_server(
    config: Mapping[str, Any],
    paths: Mapping[str, Path],
    timeout_override: float | None = None,
    failure_context: str | None = None,
) -> dict[str, Any]:
    server = require_mapping(config.get("server"), "server")
    timeout = (
        positive_float(timeout_override, "--timeout-seconds")
        if timeout_override is not None
        else positive_float(
            server.get("shutdown_timeout_seconds", 30),
            "server.shutdown_timeout_seconds",
        )
    )
    metadata_path = paths["pid_metadata"]
    configured_host, configured_port = configured_endpoint(config)
    with lifecycle_lock(metadata_path):
        metadata = read_metadata(metadata_path)
        if metadata is None:
            if port_is_open(configured_host, configured_port):
                raise VllmScriptError(
                    "No owned server metadata exists, but the configured endpoint is "
                    f"still accepting connections at {configured_host}:{configured_port}"
                )
            return {"status": "not_running", "pid_metadata_path": str(metadata_path)}
        require_same_week04_run(metadata, config, paths)
        settings = require_mapping(metadata.get("settings"), "metadata.settings")
        host = str(settings.get("host"))
        port = positive_int(settings.get("port"), "metadata.settings.port")
        if (readiness_host(host), port) != (
            readiness_host(configured_host),
            configured_port,
        ):
            raise VllmScriptError(
                "Refusing to stop server metadata for a different configured endpoint: "
                f"metadata={host}:{port}, config={configured_host}:{configured_port}"
            )
        pid, pgid, _ = validate_owned_process_group(metadata, require_leader=False)
        state, reason = ownership_status(metadata)
        if state == "not_running":
            group_alive = group_exists(pgid)
            port_open = port_is_open(host, port)
            if group_alive:
                raise VllmScriptError(
                    "Owned leader exited before shutdown, but its process group is still "
                    f"alive; refusing to signal without a live leader: pgid={pgid}; "
                    f"{shutdown_detail(pgid, host, port)}"
                )
            if port_open:
                raise VllmScriptError(
                    "Owned leader and process group exited, but the configured port is still "
                    f"held by another process: {host}:{port}"
                )
            metadata.update({"state": "stopped", "stopped_at": utc_now()})
            if failure_context:
                metadata["readiness_error"] = failure_context
            atomic_write_json(metadata_path, metadata)
            return {
                "status": "stopped",
                "pid": metadata.get("pid"),
                "detail": reason,
            }
        if state != "owned_running":
            raise VllmScriptError(
                f"Refusing to signal pid {metadata.get('pid')} ({state}: {reason}); "
                "ownership requires matching start identity and exact observed argv"
            )
        validate_owned_process_group(metadata, require_leader=True)
        signal_owned_process_group(metadata, signal.SIGTERM, require_leader=True)

    group_alive, port_open, group_disappeared = wait_for_group_and_port_closed(
        pgid, host, port, timeout
    )
    if group_alive or port_open:
        if group_disappeared:
            raise VllmScriptError(
                "Owned process group disappeared after SIGTERM, but the configured port "
                f"remains open and may now belong to another process: {host}:{port}"
            )
        # The group has remained continuously present since a fully verified
        # SIGTERM. The leader may have exited while workers stayed alive, so the
        # immutable new-session facts remain the authority for this escalation.
        signal_owned_process_group(metadata, signal.SIGKILL, require_leader=False)
        group_alive, port_open, _ = wait_for_group_and_port_closed(
            pgid, host, port, min(5.0, timeout)
        )
        if group_alive or port_open:
            raise VllmScriptError(
                "Owned server did not fully stop after SIGKILL; "
                f"{shutdown_detail(pgid, host, port)}"
            )

    with lifecycle_lock(metadata_path):
        current = read_metadata(metadata_path)
        if current is None or current.get("pid") != pid:
            raise VllmScriptError(
                "PID metadata changed before the stopped state could be recorded"
            )
        require_same_week04_run(current, config, paths)
        if current is not None and current.get("pid") == pid:
            current.update(
                {
                    "state": "stopped",
                    "stopped_at": utc_now(),
                    "gpu_compatibility": GPU_COMPATIBILITY,
                }
            )
            if failure_context:
                current["readiness_error"] = failure_context
            atomic_write_json(metadata_path, current)
    return {"status": "stopped", "pid": pid, "pgid": pgid}


def server_status(
    config: Mapping[str, Any], paths: Mapping[str, Path]
) -> tuple[dict[str, Any], int]:
    metadata = read_metadata(paths["pid_metadata"])
    if metadata is None:
        return (
            {
                "status": "not_running",
                "detail": "PID metadata does not exist",
                "pid_metadata_path": str(paths["pid_metadata"]),
                "gpu_compatibility": GPU_COMPATIBILITY,
            },
            1,
        )
    try:
        require_same_week04_run(metadata, config, paths)
        validate_owned_process_group(metadata, require_leader=False)
        state, reason = ownership_status(metadata)
    except VllmScriptError as error:
        state, reason = "not_owned", str(error)
    settings = require_mapping(metadata.get("settings"), "metadata.settings")
    host = str(settings.get("host"))
    port = positive_int(settings.get("port"), "metadata.settings.port")
    pgid = metadata.get("pgid")
    payload = {
        "status": state,
        "detail": reason,
        "metadata_state": metadata.get("state"),
        "pid": metadata.get("pid"),
        "pgid": pgid,
        "session_id": metadata.get("session_id"),
        "process_group_alive": (
            group_exists(int(pgid)) if isinstance(pgid, int) else None
        ),
        "configured_port_open": port_is_open(host, port),
        "run_id": metadata.get("run_id"),
        "pid_metadata_path": str(paths["pid_metadata"]),
        "log_path": metadata.get("log_path"),
        "gpu_compatibility": metadata.get("gpu_compatibility", GPU_COMPATIBILITY),
    }
    return payload, 0 if state == "owned_running" else 1


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan and safely manage the pinned Week 4 vLLM server."
    )
    parser.add_argument("--config", default="configs/week04.yaml")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser(
        "plan", help="Validate and print exact serve argv."
    )
    plan_parser.add_argument("--allow-non-loopback", action="store_true")

    start_parser = subparsers.add_parser("start", help="Start and wait for readiness.")
    start_parser.add_argument("--allow-non-loopback", action="store_true")
    start_parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Return after recording process ownership.",
    )

    wait_parser = subparsers.add_parser(
        "wait", help="Wait for /health then /v1/models."
    )
    wait_parser.add_argument("--timeout-seconds", type=float)

    subparsers.add_parser("status", help="Check PID identity and exact argv ownership.")

    stop_parser = subparsers.add_parser(
        "stop", help="Stop only the exactly owned process."
    )
    stop_parser.add_argument("--timeout-seconds", type=float)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config_path = resolve_config_path(args.config)
        config = load_config(config_path)
        paths = configured_paths(config)
        if args.command == "plan":
            plan, _ = build_plan(config_path, config, args.allow_non_loopback)
            result, exit_code = plan, 0
        elif args.command == "start":
            result = start_server(
                config_path, config, args.allow_non_loopback, args.no_wait
            )
            exit_code = 0
        elif args.command == "wait":
            result = wait_until_ready(config, paths, args.timeout_seconds)
            exit_code = 0
        elif args.command == "status":
            result, exit_code = server_status(config, paths)
        elif args.command == "stop":
            result = stop_server(config, paths, args.timeout_seconds)
            exit_code = 0
        else:  # argparse enforces this, but keep type checkers honest.
            raise VllmScriptError(f"Unsupported command: {args.command}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return exit_code
    except (VllmScriptError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
