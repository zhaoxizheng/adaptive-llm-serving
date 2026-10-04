"""Identity and lifecycle contract for the Week 4 vLLM experiment.

This module deliberately has no import-time dependency on vLLM, Torch, or CUDA.
GPU/runtime inspection happens only when ``collect_runtime_identity`` is called.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
import sys
import urllib.request
import uuid
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.common import read_json, stable_fingerprint, utc_now
from src.vllm_contract import (
    PINNED_VLLM_VERSION,
    config_fingerprint,
    expand_cases,
    scientific_config,
    validate_config,
)

SCHEMA_VERSION = 1
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
GIT_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def source_contract(source: Mapping[str, object]) -> dict[str, object]:
    required = (
        "git_commit",
        "git_dirty",
        "critical_source_dirty",
        "dirty_state_fingerprint",
        "source_tree_fingerprint",
        "source_files",
    )
    missing = [name for name in required if name not in source]
    if missing:
        raise ValueError(f"source identity is missing fields: {missing}")
    commit = str(source["git_commit"])
    if not GIT_COMMIT_PATTERN.fullmatch(commit):
        raise ValueError("source identity has an invalid Git commit")
    if source["critical_source_dirty"] is not False:
        raise ValueError("official Week 4 runs require clean critical source")
    files = require_mapping(source["source_files"], "source.source_files")
    if not files or any(
        not isinstance(path, str) or not SHA256_PATTERN.fullmatch(str(digest))
        for path, digest in files.items()
    ):
        raise ValueError("source identity has an invalid file inventory")
    # The ``source`` discriminator (Git checkout versus transported manifest) is
    # intentionally omitted.  Both paths represent the same source tree and must
    # remain comparable after evidence is copied off the experiment VM.
    return {name: source[name] for name in required}


def model_identity(config: Mapping[str, object]) -> dict[str, object]:
    validated = validate_config(config)
    model = validated["model"]
    return {
        "model": model["id"],
        "model_revision": model["revision"],
        "served_model_name": model["served_model_name"],
        "dtype": model["dtype"],
    }


def model_identity_fingerprint(identity: Mapping[str, object]) -> str:
    return stable_fingerprint(dict(identity), length=64)


def runtime_fingerprint(runtime: Mapping[str, object]) -> str:
    return stable_fingerprint(dict(runtime), length=64)


def _command_output(argv: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            list(argv),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"unable to execute {argv[0]}: {error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"command exited {completed.returncode}: {list(argv)!r}: {detail[:500]}"
        )
    return completed.stdout.strip()


def _gce_instance_id() -> str | None:
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/id",
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib.request.urlopen(request, timeout=0.5) as response:
            value = response.read(256).decode("ascii").strip()
    except (OSError, UnicodeError):
        return None
    return value or None


def _machine_fingerprint() -> str:
    machine_id = Path("/etc/machine-id")
    value = (
        machine_id.read_text(encoding="utf-8").strip()
        if machine_id.is_file()
        else platform.node()
    )
    if not value:
        raise RuntimeError("cannot determine a stable machine identity")
    return hashlib.sha256(value.encode()).hexdigest()


def _installed_version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def _parse_gpu_inventory(text: str) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 4 or not all(parts):
            raise RuntimeError(
                f"unexpected nvidia-smi inventory row {line_number}: {line!r}"
            )
        try:
            memory_mib = int(parts[3])
        except ValueError as error:
            raise RuntimeError(
                f"invalid GPU memory value in row {line_number}"
            ) from error
        inventory.append(
            {
                "name": parts[0],
                "uuid": parts[1],
                "driver_version": parts[2],
                "memory_total_mib": memory_mib,
            }
        )
    if not inventory:
        raise RuntimeError("nvidia-smi reported no GPUs")
    return inventory


def collect_runtime_identity(
    config: Mapping[str, object], dependency_freeze: str | Path
) -> dict[str, object]:
    """Collect and validate the runtime used by an official GPU run."""

    validated = validate_config(config)
    freeze_path = Path(dependency_freeze)
    if not freeze_path.is_file() or freeze_path.stat().st_size == 0:
        raise RuntimeError(f"missing dependency freeze: {freeze_path}")
    recorded_freeze = freeze_path.read_text(encoding="utf-8").strip()
    current_freeze = _command_output([sys.executable, "-m", "pip", "freeze", "--all"])
    if recorded_freeze != current_freeze:
        raise RuntimeError(
            "the active Python environment differs from pip-freeze.txt; "
            "rerun the Week 4 bootstrap before creating a run"
        )
    installed_vllm = _installed_version("vllm")
    if installed_vllm != PINNED_VLLM_VERSION:
        raise RuntimeError(
            f"expected vllm=={PINNED_VLLM_VERSION}, found {installed_vllm!r}"
        )
    gpu_text = _command_output(
        [
            "nvidia-smi",
            "--query-gpu=name,uuid,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ]
    )
    identity = model_identity(validated)
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_executable": str(Path(sys.executable).resolve()),
        "platform": platform.platform(),
        "packages": {
            package: _installed_version(package)
            for package in (
                "vllm",
                "torch",
                "transformers",
                "PyYAML",
                "pandas",
                "matplotlib",
            )
        },
        "vllm_version": installed_vllm,
        "dependency_freeze_sha256": sha256_file(freeze_path),
        "gpus": _parse_gpu_inventory(gpu_text),
        "machine_fingerprint": _machine_fingerprint(),
        "gce_instance_id": _gce_instance_id(),
        "model_identity": identity,
        "model_identity_fingerprint": model_identity_fingerprint(identity),
    }


def create_run_metadata(
    config: Mapping[str, object],
    source: Mapping[str, object],
    runtime: Mapping[str, object],
) -> dict[str, object]:
    validated = validate_config(config)
    identity = model_identity(validated)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(uuid.uuid4()),
        "started_at": utc_now(),
        "source": source_contract(source),
        "config_fingerprint": config_fingerprint(validated),
        "scientific_config": scientific_config(validated),
        "runtime": dict(runtime),
        "runtime_fingerprint": runtime_fingerprint(runtime),
        "model_identity": identity,
        "model_identity_fingerprint": model_identity_fingerprint(identity),
        "expected_case_count": len(expand_cases(validated)),
    }


def validate_run_metadata(
    metadata: Mapping[str, object],
    config: Mapping[str, object],
    *,
    source: Mapping[str, object] | None = None,
    runtime: Mapping[str, object] | None = None,
) -> None:
    validated = validate_config(config)
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported or missing Week 4 run metadata schema_version")
    try:
        parsed_run_id = uuid.UUID(str(metadata["run_id"]))
    except (KeyError, ValueError) as error:
        raise ValueError("Week 4 run metadata contains an invalid run_id") from error
    if str(parsed_run_id) != str(metadata["run_id"]):
        raise ValueError("Week 4 run_id is not a canonical UUID")
    expected_model = model_identity(validated)
    expected = {
        "config_fingerprint": config_fingerprint(validated),
        "scientific_config": scientific_config(validated),
        "model_identity": expected_model,
        "model_identity_fingerprint": model_identity_fingerprint(expected_model),
        "expected_case_count": len(expand_cases(validated)),
    }
    for field, value in expected.items():
        if metadata.get(field) != value:
            raise ValueError(f"Week 4 run metadata is inconsistent for {field}")
    stored_source = source_contract(
        require_mapping(metadata.get("source"), "metadata.source")
    )
    if source is not None and stored_source != source_contract(source):
        raise ValueError("cannot resume Week 4: source identity changed")
    stored_runtime = require_mapping(metadata.get("runtime"), "metadata.runtime")
    if metadata.get("runtime_fingerprint") != runtime_fingerprint(stored_runtime):
        raise ValueError("Week 4 run metadata runtime fingerprint is invalid")
    if runtime is not None:
        if dict(stored_runtime) != dict(runtime):
            raise ValueError("cannot resume Week 4: runtime identity changed")
        if metadata.get("runtime_fingerprint") != runtime_fingerprint(runtime):
            raise ValueError("cannot resume Week 4: runtime fingerprint changed")


def load_run_metadata(
    config: Mapping[str, object], path: str | Path | None = None
) -> dict[str, object]:
    validated = validate_config(config)
    output = validated["output"]
    metadata_path = Path(path or output["run_metadata"])
    if not metadata_path.is_file():
        raise ValueError(f"Week 4 run metadata is missing: {metadata_path}")
    payload = read_json(metadata_path)
    if not isinstance(payload, Mapping):
        raise ValueError("Week 4 run metadata must be a JSON object")
    result = dict(payload)
    validate_run_metadata(result, validated)
    return result


def artifact_identity(metadata: Mapping[str, object]) -> dict[str, object]:
    source = require_mapping(metadata.get("source"), "metadata.source")
    return {
        "run_id": metadata["run_id"],
        "config_fingerprint": metadata["config_fingerprint"],
        "runtime_fingerprint": metadata["runtime_fingerprint"],
        "model_identity": metadata["model_identity"],
        "model_identity_fingerprint": metadata["model_identity_fingerprint"],
        "source": {
            "git_commit": source["git_commit"],
            "source_tree_fingerprint": source["source_tree_fingerprint"],
        },
    }


def validate_artifact_identity(
    artifact: Mapping[str, object], metadata: Mapping[str, object], name: str
) -> None:
    expected = artifact_identity(metadata)
    for field, value in expected.items():
        if artifact.get(field) != value:
            raise ValueError(f"{name} identity mismatch for {field}")


def server_attempt_identity(
    artifact: Mapping[str, object], name: str
) -> dict[str, str]:
    """Return canonical logical-server and process-attempt identities."""

    result: dict[str, str] = {}
    for field in ("server_instance_id", "server_attempt_id"):
        value = artifact.get(field)
        try:
            parsed = uuid.UUID(str(value))
        except (ValueError, AttributeError) as error:
            raise ValueError(f"{name} has an invalid {field}") from error
        canonical = str(parsed)
        if str(value) != canonical:
            raise ValueError(f"{name} has a non-canonical {field}")
        result[field] = canonical
    if result["server_instance_id"] == result["server_attempt_id"]:
        raise ValueError(f"{name} reuses one UUID for instance and attempt identity")
    return result


def parse_utc_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed


def validate_resource_audit(
    audit: Mapping[str, object], metadata: Mapping[str, object]
) -> None:
    validate_artifact_identity(audit, metadata, "GPU resource audit")
    if audit.get("status") != "completed":
        raise ValueError("GPU resource audit status is not completed")
    if audit.get("vm_stopped") is not True:
        raise ValueError("GPU resource audit does not confirm VM stop")
    if audit.get("billable_resources_audited") is not True:
        raise ValueError("GPU resource audit does not confirm residual-resource audit")
    for field in ("project_id", "zone", "vm_name", "gce_instance_id"):
        value = audit.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"GPU resource audit lacks concrete {field}")
    runtime = require_mapping(metadata.get("runtime"), "metadata.runtime")
    if runtime.get("gce_instance_id") != audit.get("gce_instance_id"):
        raise ValueError("GPU resource audit instance ID differs from the run runtime")
    stopped_at = parse_utc_timestamp(audit.get("vm_stopped_at"), "audit.vm_stopped_at")
    audited_at = parse_utc_timestamp(
        audit.get("audit_completed_at"), "audit.audit_completed_at"
    )
    if audited_at < stopped_at:
        raise ValueError("resource audit completed before the VM stop timestamp")
    run_started_at = parse_utc_timestamp(
        metadata.get("started_at"), "metadata.started_at"
    )
    if stopped_at < run_started_at:
        raise ValueError("GPU resource audit VM stop predates the experiment run")
    commands = audit.get("audit_commands")
    if (
        not isinstance(commands, list)
        or not commands
        or any(
            not isinstance(command, str) or not command.strip() for command in commands
        )
    ):
        raise ValueError("GPU resource audit must record non-empty audit commands")
    resources = require_mapping(
        audit.get("audited_resources"), "audit.audited_resources"
    )
    required_resources = {
        "persistent_disks",
        "reserved_external_addresses",
        "load_balancers",
        "node_pools",
    }
    if set(resources) != required_resources:
        raise ValueError("GPU resource audit has an incomplete resource-category set")
    if any(not isinstance(resources[name], list) for name in required_resources):
        raise ValueError("GPU resource audit resource categories must be lists")
    evidence = audit.get("evidence")
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(
            not isinstance(entry, Mapping)
            or not isinstance(entry.get("path"), str)
            or not entry["path"]
            or not SHA256_PATTERN.fullmatch(str(entry.get("sha256", "")))
            for entry in evidence
        )
    ):
        raise ValueError("GPU resource audit must bind non-empty hashed evidence files")
    for entry in evidence:
        path = Path(str(entry["path"]))
        if not path.is_file() or sha256_file(path) != entry["sha256"]:
            raise ValueError(f"GPU resource audit evidence hash differs: {path}")


def environment_payload(metadata: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "valid",
        "captured_at": utc_now(),
        **artifact_identity(metadata),
        "runtime": metadata["runtime"],
        "validation_errors": [],
    }


def canonical_json_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
