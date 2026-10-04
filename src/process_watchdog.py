"""Run one command under a monotonic timeout and preserve termination evidence."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

from src.common import utc_now, write_json

TIMEOUT_EXIT_CODE = 124
INTERRUPTED_EXIT_CODE = 130


def _positive(value: float, name: str, *, allow_zero: bool = False) -> float:
    if value < 0 or (not allow_zero and value == 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {qualifier}")
    return value


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    if pgid <= 0 or pgid == os.getpgrp():
        raise RuntimeError("refusing to signal the watchdog's own process group")
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_group_exit(
    process: subprocess.Popen[bytes], pgid: int, timeout: float
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process.poll()
        if not _group_exists(pgid):
            return True
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    process.poll()
    return not _group_exists(pgid)


def run_with_watchdog(
    argv: Sequence[str],
    *,
    stdout_path: str | Path,
    timeout_seconds: float,
    term_grace_seconds: float,
) -> dict[str, object]:
    if not argv:
        raise ValueError("watchdog command cannot be empty")
    timeout = _positive(float(timeout_seconds), "timeout_seconds")
    grace = _positive(float(term_grace_seconds), "term_grace_seconds", allow_zero=True)
    destination = Path(stdout_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    started = time.monotonic()
    timed_out = False
    interrupted = False
    term_sent_at: str | None = None
    kill_sent_at: str | None = None

    with destination.open("ab", buffering=0) as output:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        pgid = process.pid
        try:
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                term_sent_at = utc_now()
                _signal_group(pgid, signal.SIGTERM)
                if not _wait_for_group_exit(process, pgid, grace):
                    kill_sent_at = utc_now()
                    _signal_group(pgid, signal.SIGKILL)
                    if not _wait_for_group_exit(
                        process, pgid, max(1.0, min(10.0, grace or 1.0))
                    ):
                        raise RuntimeError(
                            f"process group {pgid} survived SIGKILL after timeout"
                        )
                process.wait(timeout=1)
                # A timeout remains a timeout even if the child handles TERM cleanly.
                exit_code = TIMEOUT_EXIT_CODE
        except KeyboardInterrupt:
            interrupted = True
            term_sent_at = utc_now()
            _signal_group(pgid, signal.SIGTERM)
            if not _wait_for_group_exit(process, pgid, grace):
                kill_sent_at = utc_now()
                _signal_group(pgid, signal.SIGKILL)
                if not _wait_for_group_exit(
                    process, pgid, max(1.0, min(10.0, grace or 1.0))
                ):
                    raise RuntimeError(
                        f"process group {pgid} survived SIGKILL after interruption"
                    )
            process.wait(timeout=1)
            exit_code = INTERRUPTED_EXIT_CODE

    return {
        "schema_version": 1,
        "status": (
            "timed_out"
            if timed_out
            else (
                "interrupted"
                if interrupted
                else "completed" if exit_code == 0 else "failed"
            )
        ),
        "started_at": started_at,
        "finished_at": utc_now(),
        "elapsed_seconds": time.monotonic() - started,
        "timeout_seconds": timeout,
        "term_grace_seconds": grace,
        "term_sent_at": term_sent_at,
        "kill_sent_at": kill_sent_at,
        "timed_out": timed_out,
        "interrupted": interrupted,
        "exit_code": exit_code,
        "pid": process.pid,
        "process_group_id": pgid,
        "argv": list(argv),
        "stdout_path": str(destination),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--term-grace-seconds", type=float, default=10.0)
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    result = run_with_watchdog(
        command,
        stdout_path=args.stdout,
        timeout_seconds=args.timeout_seconds,
        term_grace_seconds=args.term_grace_seconds,
    )
    write_json(args.result, result)
    return int(result["exit_code"])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"watchdog failed: {error}", file=sys.stderr)
        raise SystemExit(125) from error
