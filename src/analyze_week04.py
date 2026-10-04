"""CPU-only Week 4 analysis for normalized vLLM benchmark evidence."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd

from src.common import write_json
from src.vllm_contract import server_argv, validate_config
from src.vllm_result_adapter import is_normalized_vllm_result, load_vllm_result
from src.week04_contract import artifact_identity, load_run_metadata, sha256_file

FIGURE_NAMES = (
    "concurrency-vs-throughput.png",
    "concurrency-vs-p99-ttft.png",
    "request-rate-vs-queue-time.png",
    "week03-vs-vllm-balanced.png",
)
COMPARISON_IDENTITY_FIELDS = ("model", "model_revision", "dtype")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Week 4 vLLM figures.")
    parser.add_argument("--config", default="configs/week04.yaml")
    parser.add_argument(
        "--input",
        action="append",
        required=False,
        help="Raw or normalized Week 4 JSON; repeat for multiple cases",
    )
    parser.add_argument("--week03-input", help="Week 3 summary CSV or normalized JSON")
    parser.add_argument("--week03-config", default="configs/week03.yaml")
    parser.add_argument("--output-dir")
    parser.add_argument("--summary-output")
    return parser.parse_args()


def _validate_normalized(record: Mapping[str, Any]) -> dict[str, Any]:
    required = ("source", "identity", "case", "counts", "tokens", "metrics")
    missing = [name for name in required if not isinstance(record.get(name), Mapping)]
    if missing:
        raise ValueError(f"Normalized Week 4 record is missing mappings: {missing}")
    return dict(record)


def load_week04_records(paths: Sequence[str | Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".jsonl":
            payload = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            payload = json.loads(text)
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if is_normalized_vllm_result(item):
                records.append(_validate_normalized(item))
            else:
                if len(items) != 1:
                    raise ValueError(
                        "A raw vLLM artifact must contain one result object"
                    )
                records.append(load_vllm_result(path))
    if not records:
        raise ValueError("No Week 4 records were provided")
    return records


def load_comparison_manifest(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("Week 4 comparison manifest must be a JSON object")
    records = payload.get("records")
    if (
        not isinstance(records, list)
        or not records
        or not all(isinstance(record, Mapping) for record in records)
    ):
        raise ValueError("Week 4 comparison manifest has no normalized records")
    return [_validate_normalized(record) for record in records]


def load_week03_records(
    path: str | Path, config: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    source = Path(path)
    if source.suffix.lower() == ".csv":
        if config is None:
            raise ValueError("Week 3 CSV comparison requires the Week 3 config")
        model = config.get("model")
        workload = config.get("workload")
        if not isinstance(model, Mapping) or not isinstance(workload, Mapping):
            raise ValueError("Week 3 config lacks model or workload mappings")
        with source.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        if not rows:
            raise ValueError("Week 3 comparison CSV is empty")
        return [
            {
                "schema_version": 1,
                "identity": {
                    "model": model["id"],
                    "model_revision": model["revision"],
                    "dtype": model["dtype"],
                },
                "case": {
                    "workload": "balanced",
                    "requested_prompt_tokens": int(workload["prompt_tokens"]),
                    "requested_output_tokens": int(workload["output_tokens"]),
                    "request_rate": float(row["arrival_rate_rps"]),
                    "concurrency": None,
                    "repeat": int(row["repeat"]),
                    "trace_id": row.get("trace_id"),
                    "policy": row.get("policy"),
                },
                "counts": {
                    "requested": int(row["attempted"]),
                    "success": int(row["completed"]),
                    "timeout": 0,
                    "error": int(row["rejected"]) + int(row["failed"]),
                },
                "tokens": {},
                "metrics": {
                    "request_throughput": float(row["achieved_throughput_rps"]),
                    "p99_ttft_ms": float(row["p99_ttft_ms"]),
                    "server_queue_ms": None,
                },
                "source": {"format": "week03-summary-csv"},
            }
            for row in rows
        ]
    payload = json.loads(source.read_text(encoding="utf-8"))
    records = (
        payload.get("records")
        if isinstance(payload, Mapping) and "records" in payload
        else payload
    )
    if isinstance(records, Mapping):
        records = [records]
    if (
        not isinstance(records, list)
        or not records
        or not all(isinstance(item, Mapping) for item in records)
    ):
        raise ValueError(
            "Week 3 comparison input must contain one or more record objects"
        )
    return [dict(item) for item in records]


def validate_week03_week04_comparison(
    week03_records: Sequence[Mapping[str, Any]],
    week04_records: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Return identical open-loop trace/rate cells for a balanced A/B.

    Closed-loop rows are intentionally ignored.  Concurrency is not a substitute
    for offered request rate, and a row with both dimensions populated is invalid.
    """

    def balanced_open_loop(
        records: Sequence[Mapping[str, Any]], week: str
    ) -> list[Mapping[str, Any]]:
        selected = [
            record
            for record in records
            if isinstance(record.get("case"), Mapping)
            and record["case"].get("workload") == "balanced"
            and record["case"].get("mode") in (None, "open-loop")
            and record["case"].get("request_rate") is not None
            and record["case"].get("concurrency") is None
        ]
        if not selected:
            raise ValueError(
                f"{week} has no balanced open-loop request-rate records with null concurrency"
            )
        for record in selected:
            case = record.get("case")
            identity = record.get("identity")
            metrics = record.get("metrics")
            if (
                not isinstance(case, Mapping)
                or not isinstance(identity, Mapping)
                or not isinstance(metrics, Mapping)
            ):
                raise ValueError(
                    f"{week} balanced record lacks case, identity, or metrics"
                )
            if (
                case.get("requested_prompt_tokens") != 256
                or case.get("requested_output_tokens") != 64
            ):
                raise ValueError(
                    f"{week} balanced workload must request prompt=256 and output=64"
                )
            rate = case.get("request_rate")
            if (
                isinstance(rate, bool)
                or not isinstance(rate, (int, float))
                or not math.isfinite(float(rate))
                or float(rate) <= 0
            ):
                raise ValueError(
                    f"{week} open-loop request_rate must be finite and positive"
                )
            trace_id = case.get("trace_id")
            if not isinstance(trace_id, str) or not trace_id.strip():
                raise ValueError(f"{week} open-loop record is missing case.trace_id")
            for field in COMPARISON_IDENTITY_FIELDS:
                if not identity.get(field):
                    raise ValueError(
                        f"{week} balanced record is missing identity.{field}"
                    )
            if "p99_ttft_ms" not in metrics or "request_throughput" not in metrics:
                raise ValueError(f"{week} balanced record lacks comparison metrics")
        return sorted(
            selected,
            key=lambda record: (
                float(record["case"]["request_rate"]),
                int(record["case"].get("repeat", 0)),
                str(record["case"]["trace_id"]),
            ),
        )

    left = balanced_open_loop(week03_records, "Week 3")
    right = balanced_open_loop(week04_records, "Week 4")
    expected = {
        field: left[0]["identity"][field] for field in COMPARISON_IDENTITY_FIELDS
    }
    for record in [*left, *right]:
        actual = {
            field: record["identity"][field] for field in COMPARISON_IDENTITY_FIELDS
        }
        if actual != expected:
            raise ValueError(
                f"Week 3/4 identity mismatch: expected {expected}, got {actual}"
            )
    left_shapes = {
        (
            record["case"].get("requested_prompt_tokens"),
            record["case"].get("requested_output_tokens"),
        )
        for record in left
    }
    right_shapes = {
        (
            record["case"].get("requested_prompt_tokens"),
            record["case"].get("requested_output_tokens"),
        )
        for record in right
    }
    if left_shapes != right_shapes:
        raise ValueError("Week 3/4 balanced workload shapes differ")

    def cells(records: Sequence[Mapping[str, Any]]) -> set[tuple[str, float]]:
        return {
            (str(record["case"]["trace_id"]), float(record["case"]["request_rate"]))
            for record in records
        }

    left_cells = cells(left)
    right_cells = cells(right)
    if left_cells != right_cells:
        missing = sorted(left_cells.difference(right_cells))
        extra = sorted(right_cells.difference(left_cells))
        raise ValueError(
            "Week 3/4 open-loop trace_id/request_rate cells differ: "
            f"missing_in_week04={missing}, extra_in_week04={extra}"
        )
    return left, right


def _rows(records: Sequence[Mapping[str, Any]], framework: str) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        counts = record.get("counts", {})
        requested = counts.get("requested", 0)
        success = counts.get("success", 0)
        case = record["case"]
        framework_label = (
            f"{framework}: {case.get('policy')}" if case.get("policy") else framework
        )
        rows.append(
            {
                "framework": framework_label,
                **case,
                **record["metrics"],
                "success_rate": success / requested if requested else 0.0,
            }
        )
    return rows


def _line_plot(
    frame: pd.DataFrame, x: str, y: str, output: Path, xlabel: str, ylabel: str
) -> None:
    usable = frame.dropna(subset=[x, y])
    if usable.empty:
        raise ValueError(f"No records contain both {x} and {y}")
    summary = usable.groupby(["workload", x], as_index=False)[y].median().sort_values(x)
    _, axis = plt.subplots(figsize=(9, 5))
    for workload, group in summary.groupby("workload"):
        axis.plot(group[x], group[y], marker="o", label=workload)
    axis.set_xlabel(xlabel)
    axis.set_ylabel(ylabel)
    axis.grid(alpha=0.3)
    axis.legend()
    plt.tight_layout()
    plt.savefig(output, dpi=160)
    plt.close()


def generate_week04_plots(
    week04_records: Sequence[Mapping[str, Any]],
    week03_records: Sequence[Mapping[str, Any]],
    output_dir: str | Path,
    comparison_week04_records: Sequence[Mapping[str, Any]] | None = None,
) -> list[Path]:
    left, right = validate_week03_week04_comparison(
        week03_records, comparison_week04_records or week04_records
    )
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    week04 = pd.DataFrame(_rows(week04_records, "vLLM"))
    outputs = [destination / name for name in FIGURE_NAMES]
    _line_plot(
        week04,
        "concurrency",
        "request_throughput",
        outputs[0],
        "Concurrency",
        "Median requests/s",
    )
    _line_plot(
        week04,
        "concurrency",
        "p99_ttft_ms",
        outputs[1],
        "Concurrency",
        "P99 client TTFT (ms)",
    )
    # Server queue time is plotted as its own metric and is never substituted for client TTFT.
    _line_plot(
        week04,
        "request_rate",
        "server_queue_ms",
        outputs[2],
        "Offered request rate (requests/s)",
        "Median server queue time (ms)",
    )

    comparison = pd.DataFrame(
        _rows(left, "Week 3 request batching") + _rows(right, "vLLM")
    )
    usable = comparison.dropna(subset=["request_rate", "p99_ttft_ms"])
    if usable.empty:
        raise ValueError(
            "Balanced comparison has no common request rate and P99 TTFT values"
        )
    _, axis = plt.subplots(figsize=(9, 5))
    for framework, group in usable.groupby("framework"):
        summary = (
            group.groupby("request_rate", as_index=False)["p99_ttft_ms"]
            .median()
            .sort_values("request_rate")
        )
        axis.plot(
            summary["request_rate"],
            summary["p99_ttft_ms"],
            marker="o",
            label=framework,
        )
    axis.set_xlabel("Offered request rate (requests/s)")
    axis.set_ylabel("P99 client TTFT (ms)")
    axis.grid(alpha=0.3)
    axis.legend()
    plt.tight_layout()
    plt.savefig(outputs[3], dpi=160)
    plt.close()
    return outputs


def _number(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be numeric") from error
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return result


def _record_run_id(record: Mapping[str, Any]) -> str | None:
    case = record.get("case")
    if isinstance(case, Mapping) and case.get("run_id"):
        return str(case["run_id"])
    value = record.get("run_id")
    return None if value in (None, "") else str(value)


def _argv_reference(
    record: Mapping[str, Any], raw_dir: str | Path | None
) -> dict[str, object] | None:
    case = record["case"]
    case_id = case.get("case_id")
    if not case_id or raw_dir is None:
        return None
    path = Path(raw_dir) / "benchmark" / f"{case_id}.argv.json"
    if not path.is_file():
        return None
    return {"path": str(path), "sha256": sha256_file(path)}


def build_analysis_summary(
    week04_records: Sequence[Mapping[str, Any]],
    config: Mapping[str, object],
    *,
    run_metadata: Mapping[str, object] | None = None,
    raw_dir: str | Path | None = None,
) -> dict[str, object]:
    """Build the machine-readable Week 4 SLO and operating-point summary."""

    validated = validate_config(config)
    benchmark = validated["benchmark"]
    slo = benchmark["slo"]
    open_loop = [
        record
        for record in week04_records
        if isinstance(record.get("case"), Mapping)
        and record["case"].get("workload") == "balanced"
        and record["case"].get("request_rate") is not None
        and record["case"].get("concurrency") is None
    ]
    if not open_loop:
        raise ValueError("Week 4 analysis has no balanced open-loop evidence")
    rows: list[dict[str, object]] = []
    for record in open_loop:
        case = _validate_normalized(record)["case"]
        counts = record["counts"]
        metrics = record["metrics"]
        requested = int(_number(counts.get("requested"), "counts.requested"))
        success = int(_number(counts.get("success"), "counts.success"))
        timeout = int(_number(counts.get("timeout"), "counts.timeout"))
        error = int(_number(counts.get("error"), "counts.error"))
        if requested <= 0 or success + timeout + error != requested:
            raise ValueError("Week 4 open-loop request counts are inconsistent")
        p99_ttft = _number(metrics.get("p99_ttft_ms"), "metrics.p99_ttft_ms")
        p99_tpot = _number(metrics.get("p99_tpot_ms"), "metrics.p99_tpot_ms")
        error_rate = (timeout + error) / requested
        passed = (
            p99_ttft <= float(slo["p99_ttft_ms"])
            and p99_tpot <= float(slo["p99_tpot_ms"])
            and error_rate <= float(slo["max_error_rate"])
        )
        row: dict[str, object] = {
            "case_id": case.get("case_id"),
            "run_id": _record_run_id(record),
            "trace_id": case.get("trace_id"),
            "repeat": int(case.get("repeat", 0)),
            "request_rate_rps": float(case["request_rate"]),
            "request_throughput_rps": _number(
                metrics.get("request_throughput"), "metrics.request_throughput"
            ),
            "output_token_throughput_tps": _number(
                metrics.get("output_token_throughput"),
                "metrics.output_token_throughput",
            ),
            "p99_ttft_ms": p99_ttft,
            "p99_tpot_ms": p99_tpot,
            "mean_queue_ms": (
                None
                if metrics.get("server_queue_ms") is None
                else _number(metrics["server_queue_ms"], "metrics.server_queue_ms")
            ),
            "requested": requested,
            "success": success,
            "timeout": timeout,
            "error": error,
            "error_rate": error_rate,
            "slo_pass": passed,
            "benchmark_argv": _argv_reference(record, raw_dir),
        }
        rows.append(row)
    rows.sort(
        key=lambda row: (
            float(row["request_rate_rps"]),
            int(row["repeat"]),
            str(row["case_id"]),
        ),
    )
    passing = [row for row in rows if row["slo_pass"] is True]
    selected = (
        max(
            passing,
            key=lambda row: (
                float(row["request_rate_rps"]),
                float(row["request_throughput_rps"]),
            ),
        )
        if passing
        else None
    )
    run_ids = sorted({str(row["run_id"]) for row in rows if row["run_id"]})
    execution_identities = [
        record.get("execution")
        for record in week04_records
        if isinstance(record.get("execution"), Mapping)
    ]
    server_instance_ids = sorted(
        {
            str(execution["server_instance_id"])
            for execution in execution_identities
            if execution.get("server_instance_id")
        }
    )
    server_attempt_ids = sorted(
        {
            str(execution["server_attempt_id"])
            for execution in execution_identities
            if execution.get("server_attempt_id")
        }
    )
    summary: dict[str, object] = {
        "schema_version": 1,
        "status": "completed",
        "run_id": run_ids[0] if len(run_ids) == 1 else None,
        "run_ids": run_ids,
        "server_instance_id": (
            server_instance_ids[0] if len(server_instance_ids) == 1 else None
        ),
        "server_attempt_id": (
            server_attempt_ids[0] if len(server_attempt_ids) == 1 else None
        ),
        "identity": {
            "model": validated["model"]["id"],
            "model_revision": validated["model"]["revision"],
            "dtype": validated["model"]["dtype"],
            "served_model_name": validated["model"]["served_model_name"],
        },
        "slo": {
            "p99_ttft_ms": float(slo["p99_ttft_ms"]),
            "p99_tpot_ms": float(slo["p99_tpot_ms"]),
            "max_error_rate": float(slo["max_error_rate"]),
        },
        "open_loop_case_count": len(rows),
        "open_loop_results": rows,
        "selected_operating_point": selected,
        "server_argv_reference": {
            "source": "src.vllm_contract.server_argv",
            "argv": server_argv(validated),
        },
    }
    if run_metadata is not None:
        summary.update(artifact_identity(run_metadata))
    return summary


def figure_input_sha256(
    week04_records: Sequence[Mapping[str, Any]],
    week03_records: Sequence[Mapping[str, Any]],
    comparison_week04_records: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Return deterministic hashes of the exact records feeding each figure."""

    def digest(payload: object) -> str:
        import hashlib

        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    closed = [
        record
        for record in week04_records
        if record.get("case", {}).get("concurrency") is not None
        and record.get("case", {}).get("request_rate") is None
    ]
    opened = [
        record
        for record in week04_records
        if record.get("case", {}).get("request_rate") is not None
        and record.get("case", {}).get("concurrency") is None
    ]
    left, right = validate_week03_week04_comparison(
        week03_records, comparison_week04_records
    )
    comparison = {"week03": left, "week04": right}
    return {
        FIGURE_NAMES[0]: digest(closed),
        FIGURE_NAMES[1]: digest(closed),
        FIGURE_NAMES[2]: digest(opened),
        FIGURE_NAMES[3]: digest(comparison),
    }


def main() -> None:
    args = parse_args()
    from src.common import load_yaml

    config = validate_config(load_yaml(args.config))
    output = config["output"]
    comparison = config["benchmark"]["comparison"]
    inputs = args.input
    if not inputs:
        normalized = Path(output["normalized_jsonl"])
        if normalized.is_file():
            inputs = [str(normalized)]
        else:
            inputs = [
                str(path)
                for path in sorted(Path(output["raw_dir"]).glob("benchmark/*.json"))
            ]
    week03_input = args.week03_input or comparison["week03_results"]
    week03_config = load_yaml(args.week03_config or comparison["week03_config"])
    week04_records = load_week04_records(inputs)
    comparison_manifest = Path(str(output.get("comparison_manifest", "")))
    week04_comparison_records = (
        load_comparison_manifest(comparison_manifest)
        if comparison_manifest.is_file()
        else week04_records
    )
    week03_records = load_week03_records(week03_input, week03_config)
    outputs = generate_week04_plots(
        week04_records,
        week03_records,
        args.output_dir or output["figures_dir"],
        week04_comparison_records,
    )
    metadata = None
    try:
        metadata = load_run_metadata(config)
    except (KeyError, OSError, TypeError, ValueError):
        # Unit-test and pre-run analysis remains usable; official verification
        # requires and validates run-bound metadata separately.
        metadata = None
    summary = build_analysis_summary(
        week04_records,
        config,
        run_metadata=metadata,
        raw_dir=output["raw_dir"],
    )
    summary["figures"] = [
        {
            "path": str(path),
            "sha256": sha256_file(path),
            "input_sha256": figure_input_sha256(
                week04_records, week03_records, week04_comparison_records
            )[path.name],
        }
        for path in outputs
    ]
    if comparison_manifest.is_file():
        summary["comparison_manifest"] = {
            "path": str(comparison_manifest),
            "sha256": sha256_file(comparison_manifest),
        }
    summary_path = Path(args.summary_output or output["analysis_json"])
    write_json(summary_path, summary)
    print("Wrote Week 4 figures: " + ", ".join(str(path) for path in outputs))
    print(f"Wrote Week 4 analysis summary: {summary_path}")


if __name__ == "__main__":
    main()
