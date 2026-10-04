from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path
from typing import Mapping

from src.analyze_week02 import (
    FIGURE_NAMES,
    figure_plot_input_sha256,
    summarize_results,
    summary_csv_text,
)
from src.common import load_yaml, read_json, source_identity, utc_now, write_json
from src.result_store import read_rows
from src.week02_contract import (
    RESULT_FIELDS,
    expected_cases,
    runtime_fingerprint,
    validate_canonical_matrix,
    validate_model_snapshot,
    validate_official_completion,
    validate_result_rows,
    validate_run_metadata,
)
from src.week02_smoke import validate_smoke_artifact, validate_smoke_formal_hashes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the complete Week 2 evidence package offline."
    )
    parser.add_argument("--config", default="configs/week02.yaml")
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


def _require_artifact_record(
    record: object, expected_path: Path, name: str
) -> Mapping[str, object]:
    mapping = require_mapping(record, name)
    if mapping.get("path") != str(expected_path):
        raise ValueError(f"Analysis manifest has an unexpected path for {name}")
    if mapping.get("sha256") != sha256_file(expected_path):
        raise ValueError(f"Analysis manifest hash mismatch for {name}")
    return mapping


def parse_report_evidence(report: str) -> dict[str, str]:
    match = re.search(r"<!-- WEEK02-EVIDENCE\n(?P<body>.*?)\n-->", report, re.DOTALL)
    if match is None:
        raise ValueError("Week 2 report is missing its WEEK02-EVIDENCE block")
    evidence: dict[str, str] = {}
    for line in match.group("body").splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key or not value or key in evidence:
            raise ValueError("Week 2 report has a malformed evidence block")
        evidence[key] = value
    return evidence


def verify(config_path: str | Path) -> dict[str, object]:
    config = load_yaml(config_path)
    validate_canonical_matrix(config)
    output = require_mapping(config.get("output"), "output")
    figures_dir = Path(str(output["figures_dir"]))
    required = {
        "environment": require_file(output["environment_json"]),
        "dependency_freeze": require_file(output["dependency_freeze"]),
        "model_snapshot": require_file(output["model_snapshot"]),
        "run_log": require_file(output["run_log"]),
        "run_status": require_file(output["run_status"]),
        "metadata": require_file(output["run_metadata"]),
        "smoke_json": require_file(output["smoke_json"]),
        "csv": require_file(output["raw_csv"]),
        "summary_csv": require_file(output["summary_csv"]),
        "analysis_json": require_file(output["analysis_json"]),
        "report_markdown": require_file(output["report_markdown"]),
        **{name: require_file(figures_dir / name) for name in FIGURE_NAMES},
    }

    environment = read_json(required["environment"])
    if environment.get("status") != "valid" or environment.get("validation_errors"):
        raise ValueError("environment.json did not pass CUDA/version validation")
    snapshot = read_json(required["model_snapshot"])
    snapshot_fingerprint = validate_model_snapshot(config, snapshot, verify_files=False)

    metadata = read_json(required["metadata"])
    validate_run_metadata(metadata, config)
    smoke = read_json(required["smoke_json"])
    validate_smoke_artifact(smoke, config, metadata)
    runtime = require_mapping(metadata.get("runtime"), "metadata.runtime")
    if runtime.get("dependency_freeze_sha256") != sha256_file(
        required["dependency_freeze"]
    ):
        raise ValueError("Dependency freeze differs from benchmark runtime identity")
    if runtime.get("model_snapshot_fingerprint") != snapshot_fingerprint:
        raise ValueError("Model snapshot differs from benchmark runtime identity")
    if metadata.get("runtime_fingerprint") != runtime_fingerprint(runtime):
        raise ValueError("Benchmark runtime fingerprint is internally inconsistent")

    rows = read_rows(required["csv"], expected_fields=RESULT_FIELDS)
    validate_result_rows(rows, metadata, config, require_complete=True)
    validate_smoke_formal_hashes(smoke, rows)
    validate_official_completion(rows, config)
    expected_summary = summary_csv_text(summarize_results(rows))
    if required["summary_csv"].read_text(encoding="utf-8") != expected_summary:
        raise ValueError("summary.csv is not reproducible from the raw result rows")

    analysis = read_json(required["analysis_json"])
    if analysis.get("schema_version") != 2:
        raise ValueError("Unsupported or missing Week 2 analysis schema_version")
    if analysis.get("run_id") != metadata.get("run_id"):
        raise ValueError("Analysis and benchmark run IDs differ")
    if analysis.get("terminal_case_count") != len(expected_cases(config)):
        raise ValueError("Analysis terminal case count is inconsistent")
    expected_status_counts = {
        status: sum(row["status"] == status for row in rows)
        for status in ("completed", "oom", "error")
    }
    if analysis.get("status_counts") != expected_status_counts:
        raise ValueError("Analysis status counts differ from raw result rows")
    metadata_source = require_mapping(metadata.get("source"), "metadata.source")
    if analysis.get("source_tree_fingerprint") != metadata_source.get(
        "source_tree_fingerprint"
    ):
        raise ValueError("Analysis source tree differs from benchmark metadata")
    inputs = require_mapping(analysis.get("inputs"), "analysis.inputs")
    _require_artifact_record(inputs.get("raw_csv"), required["csv"], "raw_csv")
    _require_artifact_record(
        inputs.get("run_metadata"), required["metadata"], "run_metadata"
    )
    _require_artifact_record(
        inputs.get("smoke_json"), required["smoke_json"], "smoke_json"
    )
    outputs = require_mapping(analysis.get("outputs"), "analysis.outputs")
    _require_artifact_record(
        outputs.get("summary_csv"), required["summary_csv"], "summary_csv"
    )
    figures = require_mapping(outputs.get("figures"), "analysis.outputs.figures")
    if set(figures) != set(FIGURE_NAMES):
        raise ValueError("Analysis manifest does not contain the four canonical figures")
    for name in FIGURE_NAMES:
        record = _require_artifact_record(figures[name], required[name], name)
        expected_plot_input = figure_plot_input_sha256(
            summarize_results(rows), metadata, name
        )
        if record.get("plot_input_sha256") != expected_plot_input:
            raise ValueError(f"Figure {name} is not bound to the verified plot inputs")

    environment_source = require_mapping(environment.get("source"), "environment.source")
    for field in ("git_commit", "source_tree_fingerprint"):
        if environment_source.get(field) != metadata_source.get(field):
            raise ValueError(f"Environment and benchmark source differ for {field}")
    current_source = source_identity()
    if current_source.get("source_tree_fingerprint") != metadata_source.get(
        "source_tree_fingerprint"
    ):
        raise ValueError("Current experiment code differs from benchmark source")

    status = read_json(required["run_status"])
    if status.get("status") not in {"artifacts_ready", "completed"}:
        raise ValueError("Week 2 runner did not finish successfully")
    if status.get("exit_code") != 0 or status.get("run_id") != metadata.get("run_id"):
        raise ValueError("Week 2 runner status differs from benchmark metadata")

    report = required["report_markdown"].read_text(encoding="utf-8")
    if re.search(r"<!--\s*WEEK02:[^>]+-->", report):
        raise ValueError("Week 2 report still contains pending measurement markers")
    placeholders = (
        "pending measured run",
        "Pending measured summary",
        "| pending |",
    )
    remaining = [placeholder for placeholder in placeholders if placeholder in report]
    if remaining:
        raise ValueError(f"Week 2 report still contains placeholders: {remaining}")
    required_headings = (
        "## Question",
        "## Environment",
        "## Metric Contract",
        "## Workloads",
        "## Results",
        "## Memory Model",
        "## Interpretation",
        "## Limitations",
        "## Next Steps",
    )
    missing = [heading for heading in required_headings if heading not in report]
    if missing:
        raise ValueError(f"Week 2 report is missing sections: {missing}")
    if str(metadata["run_id"]) not in report:
        raise ValueError("Week 2 report does not cite the benchmark run_id")
    for name in FIGURE_NAMES:
        if name not in report:
            raise ValueError(f"Week 2 report does not embed {name}")
    if not re.search(r"\|[^\n]+\|[^\n]+\|\n\|[-: |]+\|", report):
        raise ValueError("Week 2 report does not contain a Markdown results table")
    numeric_claims = re.findall(
        r"[^\n.!?]*\b\d+(?:\.\d+)?\s*(?:ms|MiB|tokens/s|requests/s|x|%)[^\n.!?]*[.!?]",
        report,
    )
    if len(numeric_claims) < 3:
        raise ValueError("Week 2 report needs at least three numeric conclusions with units")
    evidence = parse_report_evidence(report)
    expected_evidence = {
        "run_id": str(metadata["run_id"]),
        "raw_sha256": sha256_file(required["csv"]),
        "summary_sha256": sha256_file(required["summary_csv"]),
        "analysis_sha256": sha256_file(required["analysis_json"]),
        "smoke_sha256": sha256_file(required["smoke_json"]),
        **{
            f"figure_sha256.{name}": sha256_file(required[name])
            for name in FIGURE_NAMES
        },
    }
    if evidence != expected_evidence:
        raise ValueError("Week 2 report evidence block does not match verified artifacts")

    return {
        "run_id": metadata["run_id"],
        "case_count": len(rows),
        "expected_case_count": len(expected_cases(config)),
        "status_counts": expected_status_counts,
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
        print(f"Week 2 verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    config = load_yaml(args.config)
    receipt_path = Path(config["output"]["verification_receipt"])
    status_path = Path(config["output"]["run_status"])
    receipt = {
        "schema_version": 1,
        "verified_at": utc_now(),
        "status": "completed",
        **summary,
    }
    existing = read_json(receipt_path) if receipt_path.is_file() else None
    if isinstance(existing, Mapping):
        same_evidence = all(
            existing.get(field) == receipt.get(field)
            for field in (
                "run_id",
                "case_count",
                "expected_case_count",
                "status_counts",
                "artifact_sha256",
            )
        )
        if same_evidence:
            receipt = dict(existing)
    status = read_json(status_path)
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
    if existing != receipt:
        write_json(receipt_path, receipt)
    if read_json(status_path) != status:
        write_json(status_path, status)
    print(
        f"Week 2 evidence verified: run_id={summary['run_id']}, "
        f"cases={summary['case_count']}/{summary['expected_case_count']}, "
        f"statuses={summary['status_counts']}"
    )


if __name__ == "__main__":
    main()
