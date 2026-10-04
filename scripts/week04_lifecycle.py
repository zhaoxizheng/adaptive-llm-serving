from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping

from src.common import load_yaml, read_json, source_identity, utc_now, write_json
from src.vllm_contract import expand_cases, validate_config
from src.week04_contract import (
    artifact_identity,
    collect_runtime_identity,
    create_run_metadata,
    environment_payload,
    load_run_metadata,
    sha256_file,
    validate_artifact_identity,
    validate_run_metadata,
)

STATUS_TRANSITIONS = {
    None: {"running"},
    "running": {"running", "artifacts_ready", "failed"},
    "artifacts_ready": {"artifacts_ready", "completed", "failed"},
    # A failed/interrupted GPU attempt may resume only after initialize() has
    # revalidated the complete source/config/runtime identity below.
    "failed": {"failed", "running"},
    "completed": {"completed"},
}


def _load_config(path: str) -> dict[str, object]:
    return validate_config(load_yaml(path))


def _status_payload(
    metadata: Mapping[str, object],
    status: str,
    *,
    exit_code: int | None = None,
    phase: str | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "updated_at": utc_now(),
        "status": status,
        "exit_code": exit_code,
        "phase": phase,
        **artifact_identity(metadata),
    }
    if reason:
        payload["reason"] = reason[:4000]
    return payload


def write_status(
    path: str | Path,
    metadata: Mapping[str, object],
    status: str,
    *,
    exit_code: int | None = None,
    phase: str | None = None,
    reason: str | None = None,
) -> None:
    target = Path(path)
    existing: Mapping[str, object] | None = None
    if target.is_file():
        value = read_json(target)
        if not isinstance(value, Mapping):
            raise ValueError("Week 4 run status must be a JSON object")
        existing = value
        if value.get("run_id") != metadata.get("run_id"):
            raise ValueError("refusing to overwrite a status file for a different run")
    previous = None if existing is None else str(existing.get("status"))
    if status not in STATUS_TRANSITIONS.get(previous, set()):
        raise ValueError(
            f"invalid Week 4 status transition: {previous!r} -> {status!r}"
        )
    write_json(
        target,
        _status_payload(
            metadata, status, exit_code=exit_code, phase=phase, reason=reason
        ),
    )


def initialize(config: Mapping[str, object]) -> dict[str, object]:
    output = config["output"]
    assert isinstance(output, Mapping)
    metadata_path = Path(str(output["run_metadata"]))
    freeze_path = Path(str(output["dependency_freeze"]))
    source = source_identity()
    runtime = collect_runtime_identity(config, freeze_path)
    if metadata_path.is_file():
        value = read_json(metadata_path)
        if not isinstance(value, Mapping):
            raise ValueError("Week 4 run metadata must be a JSON object")
        metadata = dict(value)
        validate_run_metadata(metadata, config, source=source, runtime=runtime)
        status_path = Path(str(output["run_status"]))
        if status_path.is_file():
            status = read_json(status_path)
            if isinstance(status, Mapping) and status.get("status") in {
                "artifacts_ready",
                "completed",
            }:
                raise ValueError(
                    "Week 4 GPU collection is already artifacts_ready/completed; "
                    "use --analyze or --verify, or archive the run before starting a new UUID"
                )
    else:
        metadata = create_run_metadata(config, source, runtime)
        write_json(metadata_path, metadata)
    environment_path = Path(str(output["environment_json"]))
    if environment_path.is_file():
        environment = read_json(environment_path)
        if not isinstance(environment, Mapping):
            raise ValueError("Week 4 environment evidence must be a JSON object")
        validate_artifact_identity(environment, metadata, "Week 4 environment")
        if environment.get("runtime") != metadata.get("runtime"):
            raise ValueError("Week 4 environment runtime differs from run metadata")
    else:
        write_json(environment_path, environment_payload(metadata))
    write_status(
        str(output["run_status"]),
        metadata,
        "running",
        phase="initialized",
    )
    return metadata


def build_manifest(config: Mapping[str, object]) -> dict[str, object]:
    output = config["output"]
    assert isinstance(output, Mapping)
    metadata = load_run_metadata(config)
    cases = [case.to_dict() for case in expand_cases(config)]
    manifest = {
        "schema_version": 1,
        "created_at": utc_now(),
        **artifact_identity(metadata),
        "expected_case_count": len(cases),
        "cases": cases,
        "raw_artifact_contract": {
            "result": "benchmark/<case_id>.json",
            "argv": "benchmark/<case_id>.argv.json",
            "stdout": "benchmark/<case_id>.stdout.txt",
            "help": "benchmark/<case_id>.help.txt",
            "metrics_before": "metrics/<case_id>.before.prom",
            "metrics_after": "metrics/<case_id>.prom",
            "complete": "benchmark/<case_id>.complete.json",
            "incomplete": "benchmark/<case_id>.incomplete.json",
        },
    }
    write_json(str(output["benchmark_manifest"]), manifest)
    return manifest


def add_artifact_identity(
    config: Mapping[str, object], input_path: str, output_path: str | None
) -> None:
    metadata = load_run_metadata(config)
    payload = read_json(input_path)
    if not isinstance(payload, Mapping):
        raise ValueError("artifact must be a JSON object")
    enriched = dict(payload)
    enriched.update(artifact_identity(metadata))
    write_json(output_path or input_path, enriched)


def audit_template(config: Mapping[str, object], output_path: str | None) -> Path:
    output = config["output"]
    assert isinstance(output, Mapping)
    metadata = load_run_metadata(config)
    destination = Path(output_path or str(output["gpu_resource_audit"]))
    if destination.exists():
        raise ValueError(f"refusing to overwrite existing audit: {destination}")
    runtime = metadata["runtime"]
    assert isinstance(runtime, Mapping)
    write_json(
        destination,
        {
            "schema_version": 1,
            "status": "pending_manual_confirmation",
            "created_at": utc_now(),
            **artifact_identity(metadata),
            "gce_instance_id": runtime.get("gce_instance_id"),
            "project_id": None,
            "zone": None,
            "vm_name": None,
            "vm_stopped": False,
            "vm_stopped_at": None,
            "billable_resources_audited": False,
            "audit_completed_at": None,
            "audit_commands": [],
            "evidence": [],
            "audited_resources": {
                "persistent_disks": [],
                "reserved_external_addresses": [],
                "load_balancers": [],
                "node_pools": [],
            },
            "notes": (
                "Fill this only after stopping the VM and auditing the named GCP project/zone. "
                "Do not set confirmation booleans without command or console evidence."
            ),
        },
    )
    return destination


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage Week 4 run identity and status."
    )
    parser.add_argument("--config", default="configs/week04.yaml")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("initialize")
    subparsers.add_parser("manifest")
    show = subparsers.add_parser("show-id")
    show.add_argument("--field", default="run_id")
    status = subparsers.add_parser("status")
    status.add_argument(
        "--status",
        choices=("running", "artifacts_ready", "failed"),
        required=True,
    )
    status.add_argument("--exit-code", type=int)
    status.add_argument("--phase")
    status.add_argument("--reason")
    identity = subparsers.add_parser("bind-json")
    identity.add_argument("--input", required=True)
    identity.add_argument("--output")
    audit = subparsers.add_parser("audit-template")
    audit.add_argument("--output")
    hash_parser = subparsers.add_parser("sha256")
    hash_parser.add_argument("path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = _load_config(args.config)
    if args.command == "initialize":
        print(json.dumps(initialize(config), indent=2, sort_keys=True))
    elif args.command == "manifest":
        print(json.dumps(build_manifest(config), indent=2, sort_keys=True))
    elif args.command == "show-id":
        metadata = load_run_metadata(config)
        value: object = metadata
        for part in args.field.split("."):
            if not isinstance(value, Mapping) or part not in value:
                raise ValueError(f"unknown metadata field: {args.field}")
            value = value[part]
        print(value if isinstance(value, str) else json.dumps(value, sort_keys=True))
    elif args.command == "status":
        metadata = load_run_metadata(config)
        output = config["output"]
        assert isinstance(output, Mapping)
        write_status(
            str(output["run_status"]),
            metadata,
            args.status,
            exit_code=args.exit_code,
            phase=args.phase,
            reason=args.reason,
        )
    elif args.command == "bind-json":
        add_artifact_identity(config, args.input, args.output)
    elif args.command == "audit-template":
        print(audit_template(config, args.output))
    elif args.command == "sha256":
        print(sha256_file(args.path))


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"Week 4 lifecycle failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
