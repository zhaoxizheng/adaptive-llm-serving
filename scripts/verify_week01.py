from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path
from typing import Mapping

from src.common import load_yaml, read_json, source_identity, utc_now, write_json
from src.result_store import read_rows
from src.week01_contract import (
    RESULT_FIELDS,
    expected_cases,
    runtime_fingerprint,
    validate_result_rows,
    validate_model_snapshot,
    validate_run_metadata,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify Week 1 evidence and write a verification receipt."
    )
    parser.add_argument("--config", default="configs/week01.yaml")
    return parser.parse_args()


def require_file(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_file() or candidate.stat().st_size == 0:
        raise ValueError(f"Missing or empty required artifact: {candidate}")
    return candidate


def require_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(config_path: str | Path) -> dict[str, object]:
    config = load_yaml(config_path)
    output = config["output"]
    required = {
        "environment": require_file(output["environment_json"]),
        "dependency_freeze": require_file(output["dependency_freeze"]),
        "model_snapshot": require_file(output["model_snapshot"]),
        "smoke": require_file(output["smoke_json"]),
        "run_log": require_file(output["run_log"]),
        "run_status": require_file(output["run_status"]),
        "metadata": require_file(output["run_metadata"]),
        "csv": require_file(output["raw_csv"]),
        "generation_time_figure": require_file(
            Path(output["figures_dir"]) / "generation-time.png"
        ),
        "throughput_figure": require_file(
            Path(output["figures_dir"]) / "output-throughput.png"
        ),
        "report_markdown": require_file(output["report_markdown"]),
    }
    environment = read_json(required["environment"])
    if environment.get("status") != "valid" or environment.get("validation_errors"):
        raise ValueError("environment.json did not pass its CUDA/version validation")
    snapshot = read_json(required["model_snapshot"])
    snapshot_fingerprint = validate_model_snapshot(config, snapshot, verify_files=False)

    metadata = read_json(required["metadata"])
    validate_run_metadata(metadata, config)
    metadata_runtime = require_mapping(metadata.get("runtime"), "metadata.runtime")
    if metadata_runtime.get("dependency_freeze_sha256") != sha256_file(
        required["dependency_freeze"]
    ):
        raise ValueError("Dependency freeze does not match the benchmark runtime identity")
    if metadata_runtime.get("model_snapshot_fingerprint") != snapshot_fingerprint:
        raise ValueError("Model snapshot does not match the benchmark runtime identity")
    rows = read_rows(required["csv"], expected_fields=RESULT_FIELDS)
    validate_result_rows(rows, metadata, config, require_complete=True)

    smoke = read_json(required["smoke"])
    if smoke.get("status") != "completed":
        raise ValueError("Smoke artifact is not completed")
    smoke_runtime = require_mapping(smoke.get("runtime"), "smoke.runtime")
    smoke_source = require_mapping(smoke.get("source"), "smoke.source")
    metadata_source = require_mapping(metadata.get("source"), "metadata.source")
    if smoke.get("config_fingerprint") != metadata.get("config_fingerprint"):
        raise ValueError("Smoke and benchmark config fingerprints differ")
    if smoke.get("runtime_fingerprint") != runtime_fingerprint(smoke_runtime):
        raise ValueError("Smoke runtime fingerprint is internally inconsistent")
    if smoke.get("runtime_fingerprint") != metadata.get("runtime_fingerprint"):
        raise ValueError("Smoke and benchmark runtime identities differ")
    if smoke_source.get("git_commit") != metadata_source.get("git_commit"):
        raise ValueError("Smoke and benchmark Git commits differ")
    environment_source = require_mapping(environment.get("source"), "environment.source")
    if environment_source.get("git_commit") != metadata_source.get("git_commit"):
        raise ValueError("Environment and benchmark Git commits differ")
    if environment_source.get("source_tree_fingerprint") != metadata_source.get(
        "source_tree_fingerprint"
    ):
        raise ValueError("Environment and benchmark source trees differ")
    current_source = source_identity()
    if current_source.get("source_tree_fingerprint") != metadata_source.get(
        "source_tree_fingerprint"
    ):
        raise ValueError("Current experiment code differs from the benchmark source")

    status = read_json(required["run_status"])
    if status.get("status") not in {"artifacts_ready", "completed"}:
        raise ValueError("Week 1 runner did not finish successfully")
    if status.get("exit_code") != 0:
        raise ValueError("Week 1 runner status has a nonzero exit code")
    if status.get("run_id") != metadata.get("run_id"):
        raise ValueError("Runner status and benchmark run IDs differ")
    status_source = require_mapping(status.get("source"), "status.source")
    if status_source.get("git_commit") != metadata_source.get("git_commit"):
        raise ValueError("Runner status and benchmark Git commits differ")
    if status_source.get("source_tree_fingerprint") != metadata_source.get(
        "source_tree_fingerprint"
    ):
        raise ValueError("Runner status and benchmark source trees differ")

    report = required["report_markdown"].read_text(encoding="utf-8")
    placeholders = (
        "Complete after running",
        "Add the two generated figures",
        "Record observations only after",
        "Explain prefill, decode, KV cache",
    )
    remaining = [placeholder for placeholder in placeholders if placeholder in report]
    if remaining:
        raise ValueError(f"Week 1 report still contains placeholders: {remaining}")
    required_headings = (
        "## Environment",
        "## Method",
        "## Results",
        "## Observations",
        "## Limitations",
        "## What I Learned",
    )
    missing_headings = [heading for heading in required_headings if heading not in report]
    if missing_headings:
        raise ValueError(f"Week 1 report is missing sections: {missing_headings}")
    if metadata["run_id"] not in report:
        raise ValueError("Week 1 report does not cite the benchmark run_id")
    if "generation-time.png" not in report or "output-throughput.png" not in report:
        raise ValueError("Week 1 report does not embed both generated figures")
    if not re.search(r"\|[^\n]+\|[^\n]+\|\n\|[-: |]+\|", report):
        raise ValueError("Week 1 report does not contain a Markdown results table")
    if not re.search(r"\b\d+(?:\.\d+)?\s*(?:ms|MB|tokens/s|x|%)\b", report):
        raise ValueError("Week 1 report does not contain a numeric result with units")

    return {
        "run_id": metadata["run_id"],
        "case_count": len(rows),
        "expected_case_count": len(expected_cases(config)),
        "artifact_sha256": {
            name: sha256_file(path)
            for name, path in required.items()
            if name != "run_status"
        },
    }


def main() -> None:
    args = parse_args()
    try:
        summary = verify(args.config)
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(f"Week 1 verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    config = load_yaml(args.config)
    receipt_path = Path(config["output"]["verification_receipt"])
    receipt = {
        "schema_version": 1,
        "verified_at": utc_now(),
        "status": "completed",
        **summary,
    }
    existing_receipt = read_json(receipt_path) if receipt_path.is_file() else None
    if isinstance(existing_receipt, Mapping):
        same_evidence = (
            existing_receipt.get("run_id") == receipt.get("run_id")
            and existing_receipt.get("case_count") == receipt.get("case_count")
            and existing_receipt.get("expected_case_count")
            == receipt.get("expected_case_count")
            and existing_receipt.get("artifact_sha256")
            == receipt.get("artifact_sha256")
        )
        if same_evidence:
            receipt = dict(existing_receipt)
    status_path = Path(config["output"]["run_status"])
    status = read_json(status_path)
    already_completed = (
        status.get("status") == "completed"
        and status.get("exit_code") == 0
        and status.get("run_id") == summary["run_id"]
        and status.get("verification_receipt") == str(receipt_path)
    )
    if not already_completed:
        status.update(
            {
                "updated_at": utc_now(),
                "status": "completed",
                "exit_code": 0,
                "run_id": summary["run_id"],
                "verification_receipt": str(receipt_path),
            }
        )
    receipt["run_status"] = status
    if existing_receipt != receipt:
        write_json(receipt_path, receipt)
    if read_json(status_path) != status:
        write_json(status_path, status)
    print(
        f"Week 1 evidence verified: run_id={summary['run_id']}, "
        f"cases={summary['case_count']}/{summary['expected_case_count']}"
    )


if __name__ == "__main__":
    main()
