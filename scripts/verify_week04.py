from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.common import load_yaml, read_json, source_identity, utc_now, write_json
from src.analyze_week04 import (
    build_analysis_summary,
    figure_input_sha256,
    load_week03_records,
)
from src.openai_trace_client import configured_replay_cases
from src.vllm_contract import (
    PINNED_VLLM_VERSION,
    benchmark_argv,
    expand_cases,
    server_argv,
    validate_config,
)
from src.vllm_result_adapter import load_vllm_result
from src.openai_trace_client import summarize_records
from src.week03_contract import measurement_window
from src.workload import read_trace, trace_fingerprint
from src.week04_contract import (
    artifact_identity,
    load_run_metadata,
    require_mapping,
    server_attempt_identity,
    sha256_file,
    validate_artifact_identity,
    validate_resource_audit,
    validate_run_metadata,
)

FIGURES = (
    "concurrency-vs-throughput.png",
    "concurrency-vs-p99-ttft.png",
    "request-rate-vs-queue-time.png",
    "week03-vs-vllm-balanced.png",
)
CASE_SIDECARS = {
    "raw_json": "{case_id}.json",
    "argv": "{case_id}.argv.json",
    "stdout": "{case_id}.stdout.txt",
    "help": "{case_id}.help.txt",
    "watchdog": "{case_id}.watchdog.json",
    "complete": "{case_id}.complete.json",
}
MEASURED_REPORT_PATTERNS = {
    "request rate": r"(?:offered )?request rate[^\n|]*?\d+(?:\.\d+)?\s*(?:requests?/s|rps)",
    "request throughput": r"request throughput[^\n|]*?\d+(?:\.\d+)?\s*(?:requests?/s|rps)",
    "output token throughput": r"output(?:[- ]token)? throughput[^\n|]*?\d+(?:\.\d+)?\s*tokens?/s",
    "P99 TTFT": r"P99 TTFT[^\n|]*?\d+(?:\.\d+)?\s*ms",
    "P99 TPOT": r"P99 TPOT[^\n|]*?\d+(?:\.\d+)?\s*ms",
    "queue time": r"queue(?: time)?[^\n|]*?\d+(?:\.\d+)?\s*ms",
    "requested count": r"requested[^\n|]*?\d+\s*(?:requests?)?",
    "success count": r"success(?:ful)?[^\n|]*?\d+\s*(?:requests?)?",
    "timeout count": r"timeouts?[^\n|]*?\d+\s*(?:requests?)?",
    "error count": r"errors?[^\n|]*?\d+\s*(?:requests?)?",
    "error rate": r"error rate[^\n|]*?\d+(?:\.\d+)?\s*%",
}


def _required(path: str | Path) -> Path:
    result = Path(path)
    if not result.is_file() or result.stat().st_size == 0:
        raise ValueError(f"Missing or empty required artifact: {result}")
    return result


def _json_object(path: Path, name: str) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return dict(value)


def _parse_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"Invalid normalized JSONL line {line_number}: {error}"
            ) from error
        if not isinstance(value, Mapping):
            raise ValueError(f"Normalized JSONL line {line_number} is not an object")
        records.append(dict(value))
    if not records:
        raise ValueError("Normalized Week 4 result set is empty")
    return records


def _case_paths(
    raw_dir: Path,
    metrics_dir: Path,
    case_id: str,
    *,
    directory: str = "benchmark",
) -> dict[str, Path]:
    benchmark_dir = raw_dir / directory
    paths = {
        name: benchmark_dir / template.format(case_id=case_id)
        for name, template in CASE_SIDECARS.items()
    }
    paths.update(
        {
            "metrics_before": metrics_dir / f"{case_id}.before.prom",
            "metrics_after": metrics_dir / f"{case_id}.prom",
        }
    )
    return paths


def _metadata_expected(metadata: Mapping[str, Any], case: Any) -> dict[str, object]:
    source = require_mapping(metadata.get("source"), "metadata.source")
    return {
        "case_id": case.case_id,
        "run_id": metadata["run_id"],
        "git_commit": source["git_commit"],
        "source_tree_fingerprint": source["source_tree_fingerprint"],
        "config_fingerprint": metadata["config_fingerprint"],
        "runtime_fingerprint": metadata["runtime_fingerprint"],
        "model_identity_fingerprint": metadata["model_identity_fingerprint"],
        "mode": case.mode,
        "workload": case.workload,
        "requested_prompt_tokens": case.prompt_tokens,
        "requested_output_tokens": case.output_tokens,
        "repeat": case.repeat,
        "num_prompts": case.num_prompts,
    }


def _verify_case(
    raw_dir: Path,
    metrics_dir: Path,
    case: Any,
    metadata: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    server_attempt_id: str | None = None,
    directory: str = "benchmark",
    run_label: str | None = None,
    smoke: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:
    label = run_label or case.case_id
    paths = _case_paths(raw_dir, metrics_dir, label, directory=directory)
    incomplete = raw_dir / directory / f"{label}.incomplete.json"
    if incomplete.exists():
        raise ValueError(f"Case still has incomplete marker: {case.case_id}")
    required = {name: _required(path) for name, path in paths.items()}
    marker = _json_object(required["complete"], f"completion marker {case.case_id}")
    if marker.get("status") != "completed" or marker.get("case_id") != case.case_id:
        raise ValueError(f"Case completion marker is not completed: {case.case_id}")
    marker_metadata = require_mapping(marker.get("metadata"), "marker.metadata")
    expected_metadata = _metadata_expected(metadata, case)
    if smoke:
        expected_metadata.update(
            {
                "mode": "smoke",
                "benchmark_mode": case.mode,
                "num_prompts": config["benchmark"]["warmup_requests"],
            }
        )
    for field, expected in expected_metadata.items():
        if marker_metadata.get(field) != expected:
            raise ValueError(f"Case {case.case_id} metadata mismatch for {field}")
    server_identity = server_attempt_identity(
        marker_metadata, f"case {case.case_id} metadata"
    )
    if (
        server_attempt_id is not None
        and server_identity["server_attempt_id"] != server_attempt_id
    ):
        raise ValueError(f"Case {case.case_id} server attempt differs")
    marker_artifacts = require_mapping(marker.get("artifacts"), "marker.artifacts")
    expected_marker_names = set(required).difference({"complete"})
    if set(marker_artifacts) != expected_marker_names:
        raise ValueError(f"Case {case.case_id} completion marker artifact set differs")
    hashes: dict[str, str] = {}
    for name, path in required.items():
        digest = sha256_file(path)
        hashes[name] = digest
        if name == "complete":
            continue
        entry = require_mapping(marker_artifacts.get(name), f"marker.artifacts.{name}")
        if (
            entry.get("sha256") != digest
            or entry.get("size_bytes") != path.stat().st_size
        ):
            raise ValueError(f"Case {case.case_id} sidecar hash/size mismatch: {name}")
    argv = _json_object(required["argv"], f"argv sidecar {case.case_id}")
    if argv.get("metadata") != marker_metadata:
        raise ValueError(
            f"Case {case.case_id} argv metadata differs from completion marker"
        )
    observed_argv = argv.get("argv")
    if not isinstance(observed_argv, list) or not all(
        isinstance(value, str) for value in observed_argv
    ):
        raise ValueError(f"Case {case.case_id} argv sidecar is invalid")
    expected_prefix = benchmark_argv(config, case)
    if smoke:
        prompt_index = expected_prefix.index("--num-prompts")
        expected_prefix[prompt_index + 1] = str(config["benchmark"]["warmup_requests"])
    # The executable path is runtime-specific; every scientific flag and metadata
    # entry before the runner's result paths must still match exactly.
    expected_identity_metadata = [
        f"run_id={marker_metadata['run_id']}",
        f"git_commit={marker_metadata['git_commit']}",
        f"source_tree_fingerprint={marker_metadata['source_tree_fingerprint']}",
        f"config_fingerprint={marker_metadata['config_fingerprint']}",
        f"runtime_fingerprint={marker_metadata['runtime_fingerprint']}",
        f"model_identity_fingerprint={marker_metadata['model_identity_fingerprint']}",
        f"server_instance_id={marker_metadata['server_instance_id']}",
        f"server_attempt_id={marker_metadata['server_attempt_id']}",
    ]
    for value in expected_identity_metadata:
        if value not in observed_argv:
            raise ValueError(
                f"Case {case.case_id} benchmark argv lacks identity metadata {value}"
            )
    if "--result-dir" not in observed_argv or "--result-filename" not in observed_argv:
        raise ValueError(f"Case {case.case_id} benchmark argv lacks result paths")
    stripped: list[str] = []
    index = 0
    while index < len(observed_argv):
        value = observed_argv[index]
        if value in expected_identity_metadata:
            index += 1
            continue
        if value in {"--result-dir", "--result-filename"}:
            index += 2
            continue
        stripped.append(value)
        index += 1
    if stripped[1:] != expected_prefix[1:]:
        raise ValueError(f"Case {case.case_id} benchmark argv differs from contract")
    watchdog = _json_object(required["watchdog"], f"watchdog {case.case_id}")
    if (
        watchdog.get("status") != "completed"
        or watchdog.get("exit_code") != 0
        or watchdog.get("timed_out") is not False
    ):
        raise ValueError(f"Case {case.case_id} watchdog did not complete successfully")
    if smoke:
        _json_object(required["raw_json"], f"raw benchmark smoke {case.case_id}")
        normalized = {"execution": dict(server_identity)}
    else:
        normalized = load_vllm_result(required["raw_json"])
    execution = require_mapping(normalized.get("execution"), "normalized.execution")
    for field in (
        "run_id",
        "git_commit",
        "source_tree_fingerprint",
        "config_fingerprint",
        "runtime_fingerprint",
        "model_identity_fingerprint",
        "server_instance_id",
        "server_attempt_id",
    ):
        if execution.get(field) != marker_metadata.get(field):
            raise ValueError(
                f"Case {case.case_id} normalized identity mismatch: {field}"
            )
    return normalized, hashes


def _verify_smoke(
    path: Path,
    name: str,
    metadata: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    server_instance_id: str | None = None,
    server_attempt_id: str | None = None,
) -> None:
    smoke = _json_object(path, name)
    validate_artifact_identity(smoke, metadata, name)
    if smoke.get("status") != "completed":
        raise ValueError(f"{name} is not completed")
    model = config["model"]
    if smoke.get("model_revision") != model["revision"]:
        raise ValueError(f"{name} model revision differs")
    if int(smoke.get("actual_output_tokens") or 0) < 1:
        raise ValueError(f"{name} has no observed output tokens")
    if (
        server_instance_id is not None
        and smoke.get("server_instance_id") != server_instance_id
    ):
        raise ValueError(f"{name} server instance differs from benchmark evidence")
    if (
        server_attempt_id is not None
        and smoke.get("server_attempt_id") != server_attempt_id
    ):
        raise ValueError(f"{name} server attempt differs from benchmark evidence")


def _verify_comparison_manifest(
    path: Path,
    metadata: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    server_instance_id: str,
    server_attempt_id: str,
) -> dict[str, Any]:
    manifest = _json_object(path, "comparison manifest")
    validate_artifact_identity(manifest, metadata, "comparison manifest")
    if server_attempt_identity(manifest, "comparison manifest") != {
        "server_instance_id": server_instance_id,
        "server_attempt_id": server_attempt_id,
    }:
        raise ValueError("comparison manifest server attempt identity differs")
    week03_inputs = require_mapping(
        manifest.get("week03_inputs"), "comparison.week03_inputs"
    )
    for name in (
        "config",
        "run_metadata",
        "events",
        "summary",
        "verification_receipt",
        "run_status",
    ):
        entry = require_mapping(
            week03_inputs.get(name), f"comparison.week03_inputs.{name}"
        )
        evidence_path = _required(str(entry.get("path", "")))
        if entry.get("sha256") != sha256_file(evidence_path):
            raise ValueError(f"comparison Week 3 {name} hash mismatch")
    comparison_config = config["benchmark"]["comparison"]
    configured_paths = {
        "config": comparison_config["week03_config"],
        "run_metadata": comparison_config["week03_run_metadata"],
        "events": comparison_config["week03_events"],
        "summary": comparison_config["week03_results"],
        "verification_receipt": comparison_config["week03_verification_receipt"],
        "run_status": comparison_config["week03_run_status"],
    }
    for name, configured_path in configured_paths.items():
        entry = require_mapping(week03_inputs[name], f"comparison.week03_inputs.{name}")
        if Path(str(entry.get("path", ""))) != Path(str(configured_path)):
            raise ValueError(f"comparison Week 3 {name} path differs from config")
    if week03_inputs.get("arrival_trace_dir") != comparison_config["arrival_trace_dir"]:
        raise ValueError(
            "comparison Week 3 arrival trace directory differs from config"
        )
    week03_metadata_path = Path(
        str(require_mapping(week03_inputs["run_metadata"], "week03 metadata")["path"])
    )
    week03_metadata = _json_object(week03_metadata_path, "Week 3 run metadata")
    if (
        week03_metadata.get("profile") != comparison_config["profile"]
        or week03_metadata.get("backend") != comparison_config["backend"]
    ):
        raise ValueError("comparison Week 3 run does not use primary real-HF evidence")
    records = manifest.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("comparison manifest contains no trace replay records")
    expected_replays = configured_replay_cases(config)
    expected_cells = {
        (
            str(case["trace_id"]),
            str(case["trace_file_sha256"]),
            float(case["request_rate"]),
            int(case["repeat"]),
        )
        for case in expected_replays
    }
    if manifest.get("case_count") != len(expected_cells):
        raise ValueError(
            "comparison manifest case_count is not the configured replay count"
        )
    max_lag = float(config["benchmark"]["comparison"]["max_p99_arrival_lag_ms"])
    start, end = measurement_window(
        week03_metadata["scientific_config"], str(comparison_config["profile"])
    )
    seen: set[tuple[str, int]] = set()
    trace_ids: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise ValueError(f"comparison record {index} is not an object")
        if record.get("run_id") != metadata["run_id"]:
            raise ValueError("comparison records mix Week 4 run IDs")
        if (
            record.get("server_instance_id") != server_instance_id
            or record.get("server_attempt_id") != server_attempt_id
        ):
            raise ValueError("comparison records mix server attempt identities")
        case = require_mapping(record.get("case"), f"comparison.records[{index}].case")
        if (case.get("measurement_start_ns"), case.get("measurement_end_ns")) != (start, end):
            raise ValueError("comparison replay measurement window differs from Week 3")
        if (
            case.get("mode") != "open-loop"
            or case.get("workload") != "balanced"
            or case.get("concurrency") is not None
            or case.get("request_rate") is None
            or case.get("requested_prompt_tokens") != 256
            or case.get("requested_output_tokens") != 64
        ):
            raise ValueError("comparison contains a non-comparable Week 4 case")
        trace = require_mapping(
            record.get("trace"), f"comparison.records[{index}].trace"
        )
        trace_id = str(case.get("trace_id", ""))
        repeat = int(case.get("repeat"))
        if not re.fullmatch(r"[0-9a-f]{64}", trace_id):
            raise ValueError("comparison trace_id is invalid")
        if (trace_id, repeat) in seen:
            raise ValueError(
                "comparison manifest contains duplicate trace/repeat records"
            )
        seen.add((trace_id, repeat))
        trace_ids.add(trace_id)
        metrics = require_mapping(
            record.get("metrics"), f"comparison.records[{index}].metrics"
        )
        lag = float(metrics.get("p99_arrival_lag_ms", math.inf))
        if not math.isfinite(lag) or lag > max_lag:
            raise ValueError("comparison replay exceeded the client arrival-lag gate")
        if trace.get("trace_id") != trace_id:
            raise ValueError("comparison trace/case trace IDs differ")
        trace_path = _required(str(trace.get("trace_path", "")))
        if trace.get("trace_file_sha256") != sha256_file(trace_path):
            raise ValueError("comparison source trace file hash mismatch")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != len(records):
        raise ValueError("comparison manifest artifact list is incomplete")
    artifact_case_ids: set[str] = set()
    records_by_case = {
        str(record["case"].get("case_id", "")): record for record in records
    }
    for entry in artifacts:
        if not isinstance(entry, Mapping):
            raise ValueError("comparison artifact entry is not an object")
        replay_path = _required(str(entry.get("path", "")))
        if entry.get("sha256") != sha256_file(replay_path):
            raise ValueError("comparison replay artifact hash mismatch")
        case_id = str(entry.get("case_id", ""))
        artifact_case_ids.add(case_id)
        replay = _json_object(replay_path, f"trace replay {case_id}")
        validate_artifact_identity(replay, metadata, f"trace replay {case_id}")
        record = records_by_case.get(case_id)
        if record is None:
            raise ValueError(
                "comparison replay artifact has no normalized manifest record"
            )
        for field in (
            "schema_version",
            "adapter",
            "run_id",
            "config_fingerprint",
            "runtime_fingerprint",
            "model_identity",
            "model_identity_fingerprint",
            "server_instance_id",
            "server_attempt_id",
            "source",
            "trace",
            "identity",
            "case",
            "counts",
            "tokens",
            "metrics",
            "execution",
        ):
            if record.get(field) != replay.get(field):
                raise ValueError(
                    f"comparison manifest differs from replay {case_id} for {field}"
                )
        replay_trace = require_mapping(replay.get("trace"), f"replay {case_id}.trace")
        trace_path = _required(str(replay_trace.get("trace_path", "")))
        trace_rows = read_trace(trace_path)
        semantic_id = trace_fingerprint(trace_rows)
        if semantic_id != replay_trace.get("trace_id") or sha256_file(
            trace_path
        ) != replay_trace.get("trace_file_sha256"):
            raise ValueError(f"trace replay {case_id} source trace provenance differs")
        request_records = replay.get("records")
        if not isinstance(request_records, list) or len(request_records) != len(
            trace_rows
        ):
            raise ValueError(f"trace replay {case_id} request row count differs")
        for request, observed in zip(trace_rows, request_records):
            if not isinstance(observed, Mapping):
                raise ValueError(f"trace replay {case_id} contains a non-object row")
            expected_fields = {
                "trace_id": semantic_id,
                "ordinal": request.ordinal,
                "request_id": request.request_id,
                "scheduled_arrival_ns": request.scheduled_arrival_ns,
                "prompt_tokens": request.prompt_tokens,
                "output_tokens": request.output_tokens,
                "workload_class": request.workload_class,
                "measurement": request.measurement,
            }
            for field, expected in expected_fields.items():
                if observed.get(field) != expected:
                    raise ValueError(
                        f"trace replay {case_id} row {request.ordinal} differs for {field}"
                    )
        summary = summarize_records(
            request_records,
            max_p99_arrival_lag_ms=max_lag,
            measurement_start_ns=start,
            measurement_end_ns=end,
        )
        if (
            replay.get("counts") != summary["measurement_requests"]
            or replay.get("metrics") != summary["metrics"]
        ):
            raise ValueError(f"trace replay {case_id} summary differs from request evidence")
    record_case_ids = {str(record["case"].get("case_id", "")) for record in records}
    if artifact_case_ids != record_case_ids:
        raise ValueError("comparison artifacts and normalized record case IDs differ")
    if len(trace_ids) != len(records):
        raise ValueError("comparison manifest reuses a trace more than once")
    observed_cells = {
        (
            str(record["case"]["trace_id"]),
            str(record["case"]["trace_file_sha256"]),
            float(record["case"]["request_rate"]),
            int(record["case"]["repeat"]),
        )
        for record in records
    }
    if observed_cells != expected_cells:
        raise ValueError(
            "comparison manifest does not contain the exact configured replay set"
        )
    return manifest


def _verify_analysis(
    path: Path,
    metadata: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    recomputed_records: Sequence[Mapping[str, Any]],
    comparison_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    analysis = _json_object(path, "Week 4 analysis")
    validate_artifact_identity(analysis, metadata, "Week 4 analysis")
    rebuilt = build_analysis_summary(
        recomputed_records,
        config,
        run_metadata=metadata,
        raw_dir=config["output"]["raw_dir"],
    )
    for field in (
        "schema_version",
        "status",
        "run_id",
        "run_ids",
        "server_instance_id",
        "server_attempt_id",
        "identity",
        "slo",
        "open_loop_case_count",
        "open_loop_results",
        "operating_point_rule",
        "operating_point_groups",
        "selected_operating_point",
        "server_argv_reference",
        "config_fingerprint",
        "runtime_fingerprint",
        "model_identity",
        "model_identity_fingerprint",
        "source",
    ):
        if analysis.get(field) != rebuilt.get(field):
            raise ValueError(f"Week 4 analysis differs from raw evidence for {field}")
    comparison_ref = require_mapping(
        analysis.get("comparison_manifest"), "analysis.comparison_manifest"
    )
    comparison_path = _required(str(comparison_ref.get("path", "")))
    if comparison_path != Path(
        config["output"]["comparison_manifest"]
    ) or comparison_ref.get("sha256") != sha256_file(comparison_path):
        raise ValueError("Week 4 analysis comparison-manifest provenance differs")
    operating = require_mapping(
        analysis.get("selected_operating_point"),
        "analysis.selected_operating_point",
    )
    required_numeric = (
        "request_rate_rps",
        "request_throughput_rps",
        "output_token_throughput_tps",
        "p99_ttft_ms",
        "p99_tpot_ms",
        "mean_queue_ms",
        "requested",
        "success",
        "timeout",
        "error",
        "error_rate",
    )
    for field in required_numeric:
        value = operating.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError(f"analysis operating point lacks numeric {field}")
    if operating.get("slo_pass") is not True:
        raise ValueError("analysis operating point does not pass configured SLOs")
    argv_reference = require_mapping(
        analysis.get("server_argv_reference"), "analysis.server_argv_reference"
    )
    if argv_reference.get("argv") != server_argv(config):
        raise ValueError("analysis operating point server argv differs from config")
    return analysis


def _verify_report(
    path: Path, metadata: Mapping[str, Any], analysis: Mapping[str, Any]
) -> None:
    report = path.read_text(encoding="utf-8")
    placeholders = (
        "GPU compatibility: unverified",
        "TBD_AFTER_GPU_RUN",
        "NOT YET MEASURED",
    )
    remaining = [value for value in placeholders if value in report]
    if remaining:
        raise ValueError(f"Week 4 report still contains placeholders: {remaining}")
    for heading in (
        "## Environment and Version Contract",
        "## Method",
        "## Results",
        "## Week 3 Controlled Comparison",
        "## Operating Point",
        "## Limitations",
        "## Resource Cleanup",
    ):
        if heading not in report:
            raise ValueError(f"Week 4 report is missing heading: {heading}")
    if str(metadata["run_id"]) not in report:
        raise ValueError("Week 4 report does not cite the verified run UUID")
    missing_figures = [name for name in FIGURES if name not in report]
    if missing_figures:
        raise ValueError(f"Week 4 report does not cite all figures: {missing_figures}")
    operating_section = report.split("## Operating Point", 1)[1]
    operating_section = operating_section.split("\n## ", 1)[0]
    missing_numeric = [
        name
        for name, pattern in MEASURED_REPORT_PATTERNS.items()
        if not re.search(pattern, operating_section, re.I)
    ]
    if missing_numeric:
        raise ValueError(
            f"Week 4 measured operating point lacks numeric dimensions: {missing_numeric}"
        )
    if (
        "vllm serve" not in operating_section
        or "--max-num-seqs" not in operating_section
    ):
        raise ValueError(
            "Week 4 operating point does not state complete server arguments"
        )
    selected = require_mapping(
        analysis.get("selected_operating_point"), "analysis.selected_operating_point"
    )
    expected_values = {
        "request_rate_rps": ("requests/s", 6),
        "request_throughput_rps": ("requests/s", 6),
        "output_token_throughput_tps": ("tokens/s", 6),
        "p99_ttft_ms": ("ms", 6),
        "p99_tpot_ms": ("ms", 6),
        "mean_queue_ms": ("ms", 6),
    }
    for field, (unit, precision) in expected_values.items():
        value = float(selected[field])
        candidates = {
            f"{value:.{digits}f}".rstrip("0").rstrip(".")
            for digits in range(0, precision + 1)
        }
        if not any(
            re.search(
                rf"\b{re.escape(candidate)}\s*{re.escape(unit)}\b", operating_section
            )
            for candidate in candidates
        ):
            raise ValueError(
                f"Week 4 report does not match measured analysis field {field}"
            )
    for field in ("requested", "success", "timeout", "error"):
        if not re.search(rf"\b{int(selected[field])}\s*requests?\b", operating_section):
            raise ValueError(
                f"Week 4 report does not match measured analysis field {field}"
            )
    percent = float(selected["error_rate"]) * 100
    if not re.search(rf"\b{re.escape(f'{percent:g}')}\s*%", operating_section):
        raise ValueError("Week 4 report does not match measured analysis error_rate")


def verify(config_path: str | Path) -> dict[str, object]:
    config = validate_config(load_yaml(config_path))
    output = config["output"]
    metadata_path = _required(output["run_metadata"])
    metadata = load_run_metadata(config, metadata_path)
    validate_run_metadata(metadata, config)
    current_source = source_identity()
    stored_source = require_mapping(metadata["source"], "metadata.source")
    if current_source.get("source_tree_fingerprint") != stored_source.get(
        "source_tree_fingerprint"
    ):
        raise ValueError(
            "Current verification source tree differs from the GPU run source"
        )

    required = {
        "environment": _required(output["environment_json"]),
        "dependency_freeze": _required(output["dependency_freeze"]),
        "vllm_version": _required(output["vllm_version"]),
        "vllm_help": _required(output["vllm_help"]),
        "serve_help": _required(output["serve_help"]),
        "bench_help": _required(output["bench_serve_help"]),
        "offline_smoke": _required(output["offline_smoke_json"]),
        "nonstream_smoke": _required(output["nonstream_smoke_json"]),
        "stream_smoke": _required(output["stream_smoke_json"]),
        "manifest": _required(output["benchmark_manifest"]),
        "comparison_manifest": _required(output["comparison_manifest"]),
        "normalized": _required(output["normalized_jsonl"]),
        "analysis": _required(output["analysis_json"]),
        "server_process": _required(config["server"]["pid_metadata_path"]),
        "models_snapshot": _required(config["server"]["models_snapshot_path"]),
        "run_status": _required(output["run_status"]),
        "gpu_resource_audit": _required(output["gpu_resource_audit"]),
        "report": _required(output["report_markdown"]),
    }
    environment = _json_object(required["environment"], "environment")
    validate_artifact_identity(environment, metadata, "environment")
    if environment.get("status") != "valid" or environment.get("validation_errors"):
        raise ValueError("Week 4 environment did not pass validation")
    runtime = require_mapping(metadata["runtime"], "metadata.runtime")
    if runtime.get("dependency_freeze_sha256") != sha256_file(
        required["dependency_freeze"]
    ):
        raise ValueError("Dependency freeze differs from run runtime identity")
    installed_version = required["vllm_version"].read_text(encoding="utf-8").strip()
    if installed_version != PINNED_VLLM_VERSION:
        raise ValueError("vLLM version evidence does not match the pinned version")
    if runtime.get("vllm_version") != installed_version:
        raise ValueError("Runtime and version artifact disagree about vLLM")
    freeze = required["dependency_freeze"].read_text(encoding="utf-8")
    if not re.search(rf"(?im)^vllm=={re.escape(PINNED_VLLM_VERSION)}$", freeze):
        raise ValueError("Dependency freeze lacks the pinned vLLM version")

    _verify_smoke(required["offline_smoke"], "offline_smoke", metadata, config)
    offline = _json_object(required["offline_smoke"], "offline smoke")
    if offline.get("gpu_compatibility") != "verified_by_offline_smoke":
        raise ValueError("GPU compatibility was not verified by offline generation")

    manifest = _json_object(required["manifest"], "benchmark manifest")
    validate_artifact_identity(manifest, metadata, "benchmark manifest")
    expected_cases = expand_cases(config)
    declared = manifest.get("cases")
    if declared != [case.to_dict() for case in expected_cases]:
        raise ValueError("Benchmark manifest case matrix differs from the config")
    if manifest.get("expected_case_count") != len(expected_cases):
        raise ValueError("Benchmark manifest expected case count is inconsistent")

    raw_dir = Path(output["raw_dir"])
    metrics_dir = Path(output["server_metrics_dir"])
    server_process = _json_object(required["server_process"], "server process metadata")
    validate_artifact_identity(server_process, metadata, "server process metadata")
    process_identity = server_attempt_identity(
        server_process, "server process metadata"
    )
    server_instance_id = process_identity["server_instance_id"]
    server_attempt_id = process_identity["server_attempt_id"]
    if server_process.get("state") != "stopped" or not server_process.get("stopped_at"):
        raise ValueError("Server process metadata does not prove a completed shutdown")

    recomputed: list[dict[str, Any]] = []
    case_hashes: dict[str, dict[str, str]] = {}
    for case in expected_cases:
        record, hashes = _verify_case(
            raw_dir,
            metrics_dir,
            case,
            metadata,
            config,
            server_attempt_id=server_attempt_id,
        )
        recomputed.append(record)
        case_hashes[case.case_id] = hashes
    expected_ids = {case.case_id for case in expected_cases}
    allowed_json_suffixes = {"argv", "watchdog", "complete", "incomplete"}
    actual_results = {
        path.stem
        for path in (raw_dir / "benchmark").glob("*.json")
        if not any(
            path.name.endswith(f".{suffix}.json") for suffix in allowed_json_suffixes
        )
    }
    if actual_results != expected_ids:
        raise ValueError(
            f"Raw benchmark result set is not exact: {len(actual_results)}/{len(expected_ids)}"
        )
    incomplete_markers = list((raw_dir / "benchmark").glob("*.incomplete.json"))
    if incomplete_markers:
        raise ValueError(
            f"Raw benchmark directory contains incomplete markers: {incomplete_markers}"
        )
    benchmark_smoke, benchmark_smoke_hashes = _verify_case(
        raw_dir,
        metrics_dir,
        expected_cases[0],
        metadata,
        config,
        server_attempt_id=server_attempt_id,
        directory="benchmark-smoke",
        run_label=f"{expected_cases[0].case_id}-smoke",
        smoke=True,
    )
    if benchmark_smoke["execution"].get("server_instance_id") != server_instance_id:
        raise ValueError("Benchmark smoke server instance differs")

    normalized = _parse_jsonl(required["normalized"])
    if len(normalized) != len(expected_cases):
        raise ValueError("Normalized JSONL does not have the exact expected row count")
    by_case: dict[str, dict[str, Any]] = {}
    for record in normalized:
        case = require_mapping(record.get("case"), "normalized.case")
        case_id = str(case.get("case_id", ""))
        if not case_id or case_id in by_case:
            raise ValueError(
                f"Normalized JSONL has blank/duplicate case_id: {case_id!r}"
            )
        by_case[case_id] = record
    if set(by_case) != expected_ids:
        raise ValueError("Normalized JSONL case IDs differ from the expected matrix")
    for record in recomputed:
        case_id = str(record["case"]["case_id"])
        if by_case[case_id] != record:
            raise ValueError(
                f"Normalized case differs from raw re-normalization: {case_id}"
            )

    server_ids = {record["execution"]["server_instance_id"] for record in recomputed}
    if len(server_ids) != 1:
        raise ValueError("Benchmark cases mix server instance identities")
    server_instance_id = next(iter(server_ids))
    if server_process.get("server_instance_id") != server_instance_id:
        raise ValueError("Server process and benchmark server instance IDs differ")
    server_attempts = {
        record["execution"]["server_attempt_id"] for record in recomputed
    }
    if server_attempts != {server_attempt_id}:
        raise ValueError("Benchmark cases mix server attempt identities")
    configured_server_argv = server_argv(config)
    observed_server_argv = server_process.get("argv")
    if (
        not isinstance(observed_server_argv, list)
        or observed_server_argv[1:] != configured_server_argv[1:]
    ):
        raise ValueError(
            "Observed server argv differs from the configured server contract"
        )
    models_snapshot = _json_object(required["models_snapshot"], "models snapshot")
    validate_artifact_identity(models_snapshot, metadata, "models snapshot")
    if server_attempt_identity(models_snapshot, "models snapshot") != process_identity:
        raise ValueError("Readiness snapshot and server process attempts differ")
    if models_snapshot.get("status") != "ready":
        raise ValueError("Readiness snapshot is not ready")
    models_response = require_mapping(
        models_snapshot.get("response"), "models snapshot response"
    )
    advertised = {
        str(item.get("id"))
        for item in models_response.get("data", [])
        if isinstance(item, Mapping) and item.get("id") is not None
    }
    if not advertised.intersection(
        {config["model"]["id"], config["model"]["served_model_name"]}
    ):
        raise ValueError("Readiness model snapshot lacks the configured model")
    _verify_smoke(
        required["nonstream_smoke"],
        "nonstream_smoke",
        metadata,
        config,
        server_instance_id=server_instance_id,
        server_attempt_id=server_attempt_id,
    )
    _verify_smoke(
        required["stream_smoke"],
        "stream_smoke",
        metadata,
        config,
        server_instance_id=server_instance_id,
        server_attempt_id=server_attempt_id,
    )
    comparison = _verify_comparison_manifest(
        required["comparison_manifest"],
        metadata,
        config,
        server_instance_id=server_instance_id,
        server_attempt_id=server_attempt_id,
    )
    analysis = _verify_analysis(
        required["analysis"],
        metadata,
        config,
        recomputed_records=recomputed,
        comparison_manifest=comparison,
    )
    week03_config = load_yaml(config["benchmark"]["comparison"]["week03_config"])
    week03_records = load_week03_records(
        config["benchmark"]["comparison"]["week03_results"], week03_config
    )
    expected_figure_inputs = figure_input_sha256(
        recomputed, week03_records, comparison["records"]
    )
    figure_entries = analysis.get("figures")
    if not isinstance(figure_entries, list) or len(figure_entries) != len(FIGURES):
        raise ValueError("Week 4 analysis figure provenance is incomplete")
    entries_by_name = {
        Path(str(entry.get("path", ""))).name: entry
        for entry in figure_entries
        if isinstance(entry, Mapping)
    }
    for name in FIGURES:
        required[f"figure:{name}"] = _required(Path(output["figures_dir"]) / name)
        entry = entries_by_name.get(name)
        if (
            entry is None
            or entry.get("sha256") != sha256_file(required[f"figure:{name}"])
            or entry.get("input_sha256") != expected_figure_inputs[name]
        ):
            raise ValueError(f"Week 4 figure provenance differs: {name}")
    _verify_report(required["report"], metadata, analysis)

    status = _json_object(required["run_status"], "run status")
    validate_artifact_identity(status, metadata, "run status")
    if status.get("status") not in {"artifacts_ready", "completed"}:
        raise ValueError("Week 4 run status is not ready for verification")
    if status.get("exit_code") != 0:
        raise ValueError("Week 4 run status has a nonzero exit code")
    audit = _json_object(required["gpu_resource_audit"], "GPU resource audit")
    validate_resource_audit(audit, metadata)

    artifact_hashes = {
        name: sha256_file(path)
        for name, path in required.items()
        if name != "run_status"
    }
    return {
        **artifact_identity(metadata),
        "case_count": len(recomputed),
        "expected_case_count": len(expected_cases),
        "server_instance_id": server_instance_id,
        "server_attempt_id": server_attempt_id,
        "benchmark_smoke_artifact_sha256": benchmark_smoke_hashes,
        "case_artifact_sha256": case_hashes,
        "comparison_record_count": len(comparison["records"]),
        "artifact_sha256": artifact_hashes,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify completed Week 4 GPU evidence offline."
    )
    parser.add_argument("--config", default="configs/week04.yaml")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        summary = verify(args.config)
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(f"Week 4 verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    config = validate_config(load_yaml(args.config))
    output = config["output"]
    receipt_path = Path(output["verification_receipt"])
    receipt = {
        "schema_version": 1,
        "status": "completed",
        "verified_at": utc_now(),
        **summary,
    }
    # Always replace from freshly computed evidence; never retain editable fields
    # from a prior receipt.
    write_json(receipt_path, receipt)

    metadata = load_run_metadata(config)
    status_path = Path(output["run_status"])
    status = _json_object(status_path, "run status")
    status.update(
        {
            "updated_at": utc_now(),
            "status": "completed",
            "exit_code": 0,
            "phase": "verified",
            "verification_receipt": str(receipt_path),
            **artifact_identity(metadata),
        }
    )
    write_json(status_path, status)
    print(
        f"Week 4 evidence verified: run_id={summary['run_id']}, "
        f"cases={summary['case_count']}/{summary['expected_case_count']}"
    )


if __name__ == "__main__":
    main()
