from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from src.week04_contract import (
    artifact_identity,
    create_run_metadata,
    runtime_fingerprint,
    server_attempt_identity,
    source_contract,
    validate_artifact_identity,
    validate_resource_audit,
    validate_run_metadata,
)
from tests.test_vllm_contract import make_config


def make_source() -> dict[str, object]:
    files = {
        "configs/week04.yaml": "1" * 64,
        "requirements-vllm.txt": "2" * 64,
        "scripts/run_week04.sh": "3" * 64,
    }
    return {
        "git_commit": "a" * 40,
        "git_dirty": False,
        "critical_source_dirty": False,
        "dirty_state_fingerprint": "b" * 64,
        "source_tree_fingerprint": "c" * 64,
        "source_files": files,
        "source": "git",
    }


def make_runtime() -> dict[str, object]:
    return {
        "python": "3.12.3",
        "vllm_version": "0.10.2",
        "dependency_freeze_sha256": "d" * 64,
        "machine_fingerprint": "e" * 64,
        "gpus": [
            {
                "name": "NVIDIA L4",
                "uuid": "GPU-test",
                "driver_version": "570.00",
                "memory_total_mib": 23034,
            }
        ],
    }


def test_metadata_uses_uuid_and_binds_source_config_runtime_model() -> None:
    config = make_config()
    source = make_source()
    runtime = make_runtime()

    metadata = create_run_metadata(config, source, runtime)

    validate_run_metadata(metadata, config, source=source, runtime=runtime)
    assert metadata["run_id"].count("-") == 4
    assert metadata["runtime_fingerprint"] == runtime_fingerprint(runtime)
    assert metadata["source"] == source_contract(source)
    assert metadata["expected_case_count"] == 8


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda metadata: metadata.update(run_id="not-a-uuid"), "invalid run_id"),
        (
            lambda metadata: metadata.update(config_fingerprint="wrong"),
            "config_fingerprint",
        ),
        (
            lambda metadata: metadata["runtime"].update(python="3.13.0"),
            "runtime fingerprint",
        ),
        (
            lambda metadata: metadata["model_identity"].update(dtype="float16"),
            "model_identity",
        ),
    ],
)
def test_metadata_rejects_identity_tampering(mutation, message: str) -> None:
    config = make_config()
    metadata = deepcopy(create_run_metadata(config, make_source(), make_runtime()))
    mutation(metadata)

    with pytest.raises(ValueError, match=message):
        validate_run_metadata(metadata, config)


def test_artifact_identity_rejects_mixed_run_or_source() -> None:
    metadata = create_run_metadata(make_config(), make_source(), make_runtime())
    artifact = artifact_identity(metadata)
    validate_artifact_identity(artifact, metadata, "test artifact")

    changed = deepcopy(artifact)
    changed["source"]["source_tree_fingerprint"] = "f" * 64
    with pytest.raises(ValueError, match="source"):
        validate_artifact_identity(changed, metadata, "test artifact")


def test_server_attempt_identity_requires_two_distinct_canonical_uuids() -> None:
    identity = {
        "server_instance_id": "12345678-1234-4234-8234-123456789abc",
        "server_attempt_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    }
    assert server_attempt_identity(identity, "server") == identity

    with pytest.raises(ValueError, match="reuses one UUID"):
        server_attempt_identity(
            {**identity, "server_attempt_id": identity["server_instance_id"]},
            "server",
        )


def test_official_source_contract_rejects_dirty_source() -> None:
    source = make_source()
    source["critical_source_dirty"] = True
    with pytest.raises(ValueError, match="clean critical source"):
        source_contract(source)


def test_resource_audit_requires_concrete_post_stop_evidence(tmp_path) -> None:
    runtime = make_runtime()
    runtime["gce_instance_id"] = "123456789"
    metadata = create_run_metadata(make_config(), make_source(), runtime)
    started = datetime.fromisoformat(str(metadata["started_at"]))
    stopped = started + timedelta(minutes=1)
    audited = stopped + timedelta(minutes=1)
    evidence = tmp_path / "gcloud-audit.json"
    evidence.write_text("{}\n", encoding="utf-8")
    import hashlib

    audit = {
        **artifact_identity(metadata),
        "status": "completed",
        "project_id": "project-test",
        "zone": "us-central1-a",
        "vm_name": "week04",
        "gce_instance_id": "123456789",
        "vm_stopped": True,
        "vm_stopped_at": stopped.isoformat(),
        "billable_resources_audited": True,
        "audit_completed_at": audited.isoformat(),
        "audit_commands": ["gcloud compute instances describe week04 ..."],
        "evidence": [
            {
                "path": str(evidence),
                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            }
        ],
        "audited_resources": {
            "persistent_disks": [],
            "reserved_external_addresses": [],
            "load_balancers": [],
            "node_pools": [],
        },
    }

    validate_resource_audit(audit, metadata)

    audit["audit_commands"] = []
    with pytest.raises(ValueError, match="audit commands"):
        validate_resource_audit(audit, metadata)
