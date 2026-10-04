from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Mapping, Sequence

from src.analyze_week03 import (
    FIGURE_NAMES,
    analysis_payload,
    summarize_results,
    summary_csv_text,
)
from src.common import load_yaml, read_json, source_identity, utc_now, write_json
from src.week01_contract import runtime_fingerprint, validate_model_snapshot
from src.week03_calibration import (
    load_configured_calibration_artifact,
    validate_calibration_artifact,
)
from src.week03_contract import (
    BATCH_FIELDS,
    EVENT_FIELDS,
    case_id,
    expand_matrix,
    validate_case_artifacts,
    validate_official_metadata,
    validate_run_metadata,
)


def _require_file(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_file() or candidate.stat().st_size == 0:
        raise ValueError(f"missing or empty required Week 3 artifact: {candidate}")
    return candidate


def _read_csv(path: Path, fields: Sequence[str]) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(fields):
            raise ValueError(f"CSV schema mismatch for {path}")
        return list(reader)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_rows(rows: Sequence[Mapping[str, object]], fields: Sequence[str]) -> list[tuple[str, ...]]:
    return [tuple(str(row[field]) for field in fields) for row in rows]


def verify(config_path: str | Path) -> dict[str, object]:
    config = load_yaml(config_path)
    output = config["output"]
    if not isinstance(output, Mapping):
        raise ValueError("output must be a mapping")
    metadata_path = _require_file(output["run_metadata"])
    metadata = read_json(metadata_path)
    profile = str(metadata.get("profile", ""))
    backend = str(metadata.get("backend", ""))
    validate_run_metadata(metadata, config, profile=profile, backend=backend)
    validate_official_metadata(metadata)
    if (profile, backend) != ("primary", "hf"):
        raise ValueError("formal Week 3 verification requires profile=primary, backend=hf")

    required_provenance = {
        "environment": _require_file(output["environment_json"]),
        "dependency_freeze": _require_file(output["dependency_freeze"]),
        "model_snapshot": _require_file(output["model_snapshot"]),
        "calibration": _require_file(config["calibration"]["artifact"]),
        "run_status": _require_file(output["run_status"]),
    }
    environment = read_json(required_provenance["environment"])
    if environment.get("status") != "valid" or environment.get("validation_errors"):
        raise ValueError("Week 3 environment did not pass CUDA/version validation")
    environment_source = environment.get("source")
    if not isinstance(environment_source, Mapping):
        raise ValueError("Week 3 environment lacks source provenance")
    metadata_source = metadata.get("source")
    if not isinstance(metadata_source, Mapping):
        raise ValueError("Week 3 metadata lacks source provenance")
    for field in ("git_commit", "source_tree_fingerprint"):
        if environment_source.get(field) != metadata_source.get(field):
            raise ValueError(f"environment and Week 3 run differ for source {field}")
    current_source = source_identity()
    if current_source.get("source_tree_fingerprint") != metadata_source.get(
        "source_tree_fingerprint"
    ):
        raise ValueError("current experiment code differs from Week 3 benchmark source")
    runtime = metadata.get("runtime")
    if not isinstance(runtime, Mapping):
        raise ValueError("Week 3 metadata lacks runtime provenance")
    if metadata.get("runtime_fingerprint") != runtime_fingerprint(runtime):
        raise ValueError("Week 3 runtime fingerprint is internally inconsistent")
    if runtime.get("dependency_freeze_sha256") != _sha256(
        required_provenance["dependency_freeze"]
    ):
        raise ValueError("dependency freeze differs from Week 3 runtime identity")
    snapshot = read_json(required_provenance["model_snapshot"])
    snapshot_fingerprint = validate_model_snapshot(config, snapshot, verify_files=False)
    if runtime.get("model_snapshot_fingerprint") != snapshot_fingerprint:
        raise ValueError("model snapshot differs from Week 3 runtime identity")
    calibration = load_configured_calibration_artifact(config)
    validate_calibration_artifact(config, calibration)
    if metadata.get("calibration") != calibration:
        raise ValueError("Week 3 metadata is not bound to the measured calibration artifact")
    expected = expand_matrix(config, profile)

    root = Path(str(output["root"]))
    aggregate_events_path = _require_file(output["raw_events_csv"])
    aggregate_batches_path = _require_file(output["raw_batches_csv"])
    aggregate_events = _read_csv(aggregate_events_path, EVENT_FIELDS)
    aggregate_batches = _read_csv(aggregate_batches_path, BATCH_FIELDS)
    case_events: list[dict[str, str]] = []
    case_batches: list[dict[str, str]] = []
    trace_ids: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    for case in expected:
        directory = root / "raw" / "cases" / case_id(case)
        marker_path = _require_file(directory / "complete.json")
        events_path = _require_file(directory / "events.csv")
        batches_path = _require_file(directory / "batches.csv")
        marker = read_json(marker_path)
        marker_identity = {
            "run_id": metadata["run_id"],
            "metadata_fingerprint": metadata["metadata_fingerprint"],
            "config_fingerprint": metadata["config_fingerprint"],
            "calibration_id": metadata["calibration_id"],
            "case_id": case_id(case),
        }
        if any(marker.get(field) != value for field, value in marker_identity.items()):
            raise ValueError(f"case completion identity mismatch: {case_id(case)}")
        hashes = marker.get("sha256")
        if not isinstance(hashes, Mapping) or hashes.get("events.csv") != _sha256(events_path) or hashes.get("batches.csv") != _sha256(batches_path):
            raise ValueError(f"case completion hash mismatch: {case_id(case)}")
        events = _read_csv(events_path, EVENT_FIELDS)
        batches = _read_csv(batches_path, BATCH_FIELDS)
        marker_trace_id = str(marker.get("trace_id", ""))
        if not marker_trace_id:
            raise ValueError(f"case completion lacks trace identity: {case_id(case)}")
        validate_case_artifacts(
            events,
            batches,
            expected_run_id=str(metadata["run_id"]),
            expected_case_id=case_id(case),
            expected_metadata=metadata,
            expected_case=case,
            expected_trace_id=marker_trace_id,
        )
        if any(row["status"] == "failed" for row in events) or any(
            row["status"] == "failed" for row in batches
        ):
            raise ValueError(f"formal HF case contains failed evidence: {case_id(case)}")
        case_events.extend(events)
        case_batches.extend(batches)
        if events:
            trace_ids[(case.profile, case.offered_load_ratio, str(case.repeat))].add(events[0]["trace_id"])

    if any(len(values) != 1 for values in trace_ids.values()):
        raise ValueError("policy A/B cases did not reuse an identical trace")
    if _canonical_rows(aggregate_events, EVENT_FIELDS) != _canonical_rows(case_events, EVENT_FIELDS):
        raise ValueError("aggregate events.csv is not the ordered union of complete cases")
    if _canonical_rows(aggregate_batches, BATCH_FIELDS) != _canonical_rows(case_batches, BATCH_FIELDS):
        raise ValueError("aggregate batches.csv is not the ordered union of complete cases")

    summaries = summarize_results(aggregate_events, aggregate_batches, metadata=metadata)
    summary_path = _require_file(output["summary_csv"])
    if summary_path.read_text(encoding="utf-8") != summary_csv_text(summaries):
        raise ValueError("Week 3 summary.csv is not reproducible from raw evidence")
    if len(summaries) != len(expected):
        raise ValueError("Week 3 summary does not contain the complete matrix")
    analysis_path = _require_file(output["analysis_json"])
    figures = [
        _require_file(Path(str(output["figures_dir"])) / name) for name in FIGURE_NAMES
    ]
    expected_analysis = analysis_payload(
        summaries,
        metadata=metadata,
        events_sha256=_sha256(aggregate_events_path),
        batches_sha256=_sha256(aggregate_batches_path),
        figures=figures,
    )
    if read_json(analysis_path) != expected_analysis:
        raise ValueError("Week 3 analysis.json is not reproducible from raw evidence")
    run_status = read_json(required_provenance["run_status"])
    if run_status.get("status") not in {
        "benchmark_complete",
        "artifacts_ready",
        "completed",
    } or run_status.get("run_id") != metadata.get("run_id"):
        raise ValueError("Week 3 runner status is incomplete or belongs to another run")
    report_path = _require_file(output["report_markdown"])
    report = report_path.read_text(encoding="utf-8")
    required_headings = (
        "## Run Identity",
        "## Method",
        "## Results",
        "## Overload and Tail Latency",
        "## Limitations",
        "## Week 4 Hypotheses",
    )
    missing = [heading for heading in required_headings if heading not in report]
    if missing:
        raise ValueError(f"Week 3 report is missing sections: {missing}")
    placeholders = ("TODO_FROM_EVIDENCE", "RUN_ID_FROM_METADATA", "Complete after running")
    remaining = [value for value in placeholders if value in report]
    if remaining:
        raise ValueError(f"Week 3 report still contains placeholders: {remaining}")
    if str(metadata["run_id"]) not in report:
        raise ValueError("Week 3 report does not cite the run_id")
    if any(name not in report for name in FIGURE_NAMES):
        raise ValueError("Week 3 report does not embed all four figures")
    required_terms = ("arrival lag", "rejection", "failed", "continuous batching")
    lowered = report.lower()
    if any(term not in lowered for term in required_terms):
        raise ValueError("Week 3 report omits a required metric or scope boundary")
    if not re.search(r"\b\d+(?:\.\d+)?\s*(?:ms|requests/s|%|x)\b", report):
        raise ValueError("Week 3 report needs a numeric result with units")

    artifacts = {
        "metadata": metadata_path,
        "events": aggregate_events_path,
        "batches": aggregate_batches_path,
        "summary": summary_path,
        "analysis": analysis_path,
        **required_provenance,
        "report": report_path,
        **{name: path for name, path in zip(FIGURE_NAMES, figures)},
    }
    return {
        "run_id": metadata["run_id"],
        "profile": profile,
        "backend": backend,
        "metadata_fingerprint": metadata["metadata_fingerprint"],
        "calibration_id": metadata["calibration_id"],
        "case_count": len(expected),
        "event_count": len(aggregate_events),
        "batch_count": len(aggregate_batches),
        "artifact_sha256": {name: _sha256(path) for name, path in artifacts.items()},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify Week 3 evidence offline.")
    parser.add_argument("--config", default="configs/week03.yaml")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        summary = verify(args.config)
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(f"Week 3 verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    config = load_yaml(args.config)
    output = config["output"]
    receipt_path = Path(str(output["verification_receipt"]))
    receipt = {"schema_version": 2, "verified_at": utc_now(), "status": "completed", **summary}
    existing = read_json(receipt_path) if receipt_path.is_file() else None
    if isinstance(existing, Mapping):
        stable_fields = (
            "run_id",
            "profile",
            "backend",
            "metadata_fingerprint",
            "calibration_id",
            "case_count",
            "event_count",
            "batch_count",
            "artifact_sha256",
        )
        if all(existing.get(field) == receipt.get(field) for field in stable_fields):
            receipt = dict(existing)
    if existing != receipt:
        write_json(receipt_path, receipt)
    status_path = Path(str(output["run_status"]))
    status = read_json(status_path) if status_path.is_file() else {}
    status.update({"updated_at": utc_now(), "status": "completed", "exit_code": 0, "run_id": summary["run_id"], "verification_receipt": str(receipt_path)})
    current_status = read_json(status_path) if status_path.is_file() else None
    if current_status != status:
        write_json(status_path, status)
    print(f"Week 3 evidence verified: run_id={summary['run_id']}, cases={summary['case_count']}")


if __name__ == "__main__":
    main()
