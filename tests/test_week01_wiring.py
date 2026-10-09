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


def test_gcp_transfer_scripts_use_explicit_remote_user() -> None:
    repository = Path(__file__).resolve().parents[1]
    expected_default = 'GCP_SSH_USER="${GCP_SSH_USER:-llmlearner}"'
    expected_target = '${GCP_SSH_USER}@${GCP_VM_NAME}'

    for relative_path in [
        "scripts/gcp_vm.sh",
        "scripts/upload_to_gcp.sh",
        "scripts/sync_results_from_gcp.sh",
    ]:
        content = (repository / relative_path).read_text(encoding="utf-8")
        assert expected_default in content
        assert expected_target in content
