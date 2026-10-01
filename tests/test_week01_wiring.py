from __future__ import annotations

import subprocess
from pathlib import Path


def test_make_verify_forwards_python_and_config() -> None:
    repository = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [
            "make",
            "--dry-run",
            "verify",
            "PYTHON=.test-python",
            "CONFIG=configs/test-week01.yaml",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )

    assert (
        ".test-python -m scripts.verify_week01 --config configs/test-week01.yaml"
        in completed.stdout.splitlines()
    )
