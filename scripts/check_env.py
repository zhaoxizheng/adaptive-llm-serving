from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path

from src.common import git_commit, utc_now, write_json


def command_output(command: list[str]) -> str | None:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.STDOUT).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def package_version(name: str) -> str | None:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record the experiment environment.")
    parser.add_argument("--output", default="results/week01/environment.json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = {
        "captured_at": utc_now(),
        "git_commit": git_commit(),
        "platform": platform.platform(),
        "python": sys.version,
        "nvidia_smi": command_output(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total",
                "--format=csv,noheader",
            ]
        ),
        "packages": {
            name: package_version(name)
            for name in ["torch", "transformers", "accelerate", "pandas", "matplotlib"]
        },
    }
    try:
        import torch

        payload["cuda"] = {
            "available": torch.cuda.is_available(),
            "runtime": torch.version.cuda,
            "device_count": torch.cuda.device_count(),
            "devices": [
                torch.cuda.get_device_name(index) for index in range(torch.cuda.device_count())
            ],
        }
    except ImportError:
        payload["cuda"] = {"available": False, "error": "torch is not installed"}

    write_json(Path(args.output), payload)
    print(Path(args.output).read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()

