from __future__ import annotations

import argparse
import platform
import re
import subprocess
import sys
from pathlib import Path

from src.common import load_yaml, require_clean_source, source_identity, utc_now, write_json


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


def gce_metadata(path: str) -> str | None:
    return command_output(
        [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--connect-timeout",
            "1",
            "--max-time",
            "2",
            "-H",
            "Metadata-Flavor: Google",
            f"http://metadata.google.internal/computeMetadata/v1/{path}",
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record the experiment environment.")
    parser.add_argument("--config", default="configs/week01.yaml")
    parser.add_argument("--output")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_yaml(args.config)
    output_path = Path(args.output or config["output"]["environment_json"])
    source = source_identity()
    require_clean_source(source)
    payload = {
        "schema_version": 1,
        "captured_at": utc_now(),
        "source": source,
        "platform": platform.platform(),
        "python": sys.version,
        "gce": {
            "instance_id": gce_metadata("instance/id"),
            "image": gce_metadata("instance/image"),
            "machine_type": gce_metadata("instance/machine-type"),
            "zone": gce_metadata("instance/zone"),
        },
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
            "capabilities": [
                list(torch.cuda.get_device_capability(index))
                for index in range(torch.cuda.device_count())
            ],
        }
    except ImportError:
        payload["cuda"] = {"available": False, "error": "torch is not installed"}

    errors = []
    if sys.version_info[:2] != (3, 12):
        errors.append(f"expected Python 3.12, got {platform.python_version()}")
    if not payload["nvidia_smi"]:
        errors.append("nvidia-smi did not return GPU information")
    if not payload["cuda"].get("available"):
        errors.append("PyTorch CUDA is unavailable")
    expected_packages = {
        "torch": r"^2\.8\.0(?:\+cu128)?$",
        "transformers": r"^4\.46\.3$",
        "accelerate": r"^1\.1\.1$",
        "pandas": r"^2\.2\.3$",
        "matplotlib": r"^3\.9\.2$",
    }
    for package, pattern in expected_packages.items():
        version = payload["packages"].get(package)
        if version is None or re.fullmatch(pattern, version) is None:
            errors.append(f"expected {package} matching {pattern}, got {version!r}")
    payload["status"] = "valid" if not errors else "invalid"
    payload["validation_errors"] = errors
    write_json(output_path, payload)
    print(output_path.read_text(encoding="utf-8"))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

