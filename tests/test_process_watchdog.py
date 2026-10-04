from __future__ import annotations

import sys

from src.process_watchdog import TIMEOUT_EXIT_CODE, run_with_watchdog


def test_watchdog_records_success(tmp_path) -> None:
    result = run_with_watchdog(
        [sys.executable, "-c", "print('done')"],
        stdout_path=tmp_path / "stdout.txt",
        timeout_seconds=2,
        term_grace_seconds=0.2,
    )

    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert (tmp_path / "stdout.txt").read_text(encoding="utf-8") == "done\n"


def test_watchdog_times_out_process_group_and_preserves_stdout(tmp_path) -> None:
    result = run_with_watchdog(
        [
            sys.executable,
            "-c",
            "import time; print('started', flush=True); time.sleep(30)",
        ],
        stdout_path=tmp_path / "stdout.txt",
        timeout_seconds=0.1,
        term_grace_seconds=0.2,
    )

    assert result["status"] == "timed_out"
    assert result["timed_out"] is True
    assert result["exit_code"] == TIMEOUT_EXIT_CODE
    assert result["term_sent_at"]
    assert "started" in (tmp_path / "stdout.txt").read_text(encoding="utf-8")
