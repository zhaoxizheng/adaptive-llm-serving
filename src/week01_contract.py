from __future__ import annotations

import math
import platform
import re
import subprocess
import sys
import uuid
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Mapping

from src.common import read_json, stable_fingerprint, utc_now
from src.result_store import case_key, parse_bool

SCHEMA_VERSION = 1
PINNED_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")

RESULT_FIELDS = [
    "timestamp",
    "run_id",
    "git_commit",
    "config_fingerprint",
    "runtime_fingerprint",
    "model",
    "model_revision",
    "dtype",
    "repeat",
    "use_cache",
    "prompt_tokens",
    "output_tokens",
    "tokenization_ms",
    "h2d_ms",
    "prefill_forward_ms",
    "first_token_selection_ms",
    "inference_ttft_ms",
    "end_to_end_ttft_ms",
    "decode_ms",
    "mean_tpot_ms",
    "p50_tpot_ms",
    "p95_tpot_ms",
    "total_generation_ms",
    "end_to_end_ms",
    "output_tokens_per_second",
    "peak_memory_mb",
    "output_token_hash",
]

IDENTITY_FIELDS = {
    "run_id",
    "git_commit",
    "config_fingerprint",
    "runtime_fingerprint",
    "model",
    "model_revision",
    "dtype",
}
INTEGER_FIELDS = {"repeat", "prompt_tokens", "output_tokens"}
FLOAT_FIELDS = {
    "tokenization_ms",
    "h2d_ms",
    "prefill_forward_ms",
    "first_token_selection_ms",
    "inference_ttft_ms",
    "end_to_end_ttft_ms",
    "decode_ms",
    "total_generation_ms",
    "end_to_end_ms",
    "output_tokens_per_second",
    "peak_memory_mb",
}
TPOT_FIELDS = {"mean_tpot_ms", "p50_tpot_ms", "p95_tpot_ms"}


def scientific_config(config: Mapping[str, object]) -> dict[str, object]:
    required = ("model", "generation", "benchmark")
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(f"Week 1 config is missing sections: {missing}")
    snapshot = {key: deepcopy(config[key]) for key in required}
    model = snapshot["model"]
    if not isinstance(model, dict):
        raise ValueError("model must be a mapping")
    revision = str(model["revision"])
    if not PINNED_REVISION_PATTERN.fullmatch(revision):
        raise ValueError(
            "model.revision must be an immutable 40-character Hugging Face commit SHA"
        )
    return snapshot


def config_fingerprint(config: Mapping[str, object]) -> str:
    return stable_fingerprint(scientific_config(config))


def expected_cases(config: Mapping[str, object]) -> set[tuple[int, int, int, bool]]:
    benchmark = config["benchmark"]
    if not isinstance(benchmark, dict):
        raise ValueError("benchmark must be a mapping")
    return {
        (int(prompt_tokens), int(output_tokens), repeat, bool(use_cache))
        for prompt_tokens in benchmark["prompt_tokens"]
        for output_tokens in benchmark["output_tokens"]
        for repeat in range(int(benchmark["repeats"]))
        for use_cache in benchmark["cache_modes"]
    }


def _command_output(command: list[str]) -> str | None:
    try:
        return subprocess.check_output(
            command, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def model_snapshot_fingerprint(payload: Mapping[str, object]) -> str:
    identity = {
        "model": payload.get("model"),
        "requested_revision": payload.get("requested_revision"),
        "resolved_revision": payload.get("resolved_revision"),
        "files": payload.get("files"),
    }
    return stable_fingerprint(identity, length=64)


def validate_model_snapshot(
    config: Mapping[str, object],
    payload: Mapping[str, object],
    *,
    verify_files: bool = True,
) -> str:
    model = config.get("model")
    if not isinstance(model, Mapping):
        raise ValueError("model must be a mapping")
    if payload.get("model") != model.get("id"):
        raise ValueError("Model snapshot repository does not match the config")
    revision = str(model.get("revision", ""))
    if (
        payload.get("requested_revision") != revision
        or payload.get("resolved_revision") != revision
    ):
        raise ValueError("Model snapshot revision does not match the config")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("Model snapshot file inventory is empty")
    inventoried_paths: set[str] = set()
    for entry in files:
        if not isinstance(entry, Mapping):
            raise ValueError("Model snapshot inventory entry must be a mapping")
        relative_path = str(entry.get("path", ""))
        if not relative_path or relative_path in inventoried_paths:
            raise ValueError(f"Invalid or duplicate model snapshot path: {relative_path!r}")
        inventoried_paths.add(relative_path)
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
            raise ValueError(f"Model snapshot has an invalid file hash: {relative_path}")
        size_bytes = entry.get("size_bytes")
        if not isinstance(size_bytes, int) or size_bytes < 0:
            raise ValueError(f"Model snapshot has an invalid file size: {relative_path}")
    if verify_files:
        snapshot_path = Path(str(payload.get("snapshot_path", "")))
        if not snapshot_path.is_dir():
            raise ValueError(f"Model snapshot directory is missing: {snapshot_path}")
        for entry in files:
            relative_path = str(entry["path"])
            path = snapshot_path / relative_path
            if not path.is_file():
                raise ValueError(f"Model snapshot file is missing: {relative_path}")
            if path.stat().st_size != entry["size_bytes"]:
                raise ValueError(f"Model snapshot file size changed: {relative_path}")
            if _file_sha256(path) != entry["sha256"]:
                raise ValueError(f"Model snapshot file hash changed: {relative_path}")
        current_paths = {
            str(path.relative_to(snapshot_path))
            for path in snapshot_path.rglob("*")
            if path.is_file()
        }
        if current_paths != inventoried_paths:
            raise ValueError("Model snapshot file set differs from its inventory")
    fingerprint = model_snapshot_fingerprint(payload)
    if payload.get("snapshot_fingerprint") != fingerprint:
        raise ValueError("Model snapshot aggregate fingerprint is invalid")
    return fingerprint


def collect_runtime_identity(config: Mapping[str, object]) -> dict[str, object]:
    import torch
    import transformers

    if not torch.cuda.is_available():
        raise RuntimeError("A CUDA GPU is required for the Week 1 experiment.")
    model = config["model"]
    if not isinstance(model, dict):
        raise ValueError("model must be a mapping")
    requested_dtype = str(model["dtype"])
    if requested_dtype == "auto":
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    elif requested_dtype == "bfloat16":
        dtype = torch.bfloat16
    elif requested_dtype == "float16":
        dtype = torch.float16
    else:
        raise ValueError(f"Unsupported dtype: {requested_dtype}")
    output = config.get("output")
    if not isinstance(output, Mapping):
        raise ValueError("output must be a mapping")
    freeze_path = Path(str(output["dependency_freeze"]))
    if not freeze_path.is_file():
        raise RuntimeError(
            f"Missing dependency freeze {freeze_path}; rerun the bootstrap before the experiment."
        )
    current_freeze = _command_output([sys.executable, "-m", "pip", "freeze", "--all"])
    recorded_freeze = freeze_path.read_text(encoding="utf-8").strip()
    if current_freeze is None or current_freeze.strip() != recorded_freeze:
        raise RuntimeError(
            "Installed packages differ from dependency-freeze.txt; rerun the bootstrap "
            "or start a new experiment run."
        )
    snapshot_path = Path(str(output["model_snapshot"]))
    if not snapshot_path.is_file():
        raise RuntimeError(
            f"Missing model snapshot evidence {snapshot_path}; run make prepare-model first."
        )
    snapshot = read_json(snapshot_path)
    snapshot_fingerprint = validate_model_snapshot(config, snapshot, verify_files=False)
    machine_id_path = Path("/etc/machine-id")
    machine_id = (
        machine_id_path.read_text(encoding="utf-8").strip()
        if machine_id_path.is_file()
        else None
    )
    gce_instance_id = _command_output(
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
            "http://metadata.google.internal/computeMetadata/v1/instance/id",
        ]
    )
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "pytorch": torch.__version__,
        "transformers": transformers.__version__,
        "packages": {
            name: _package_version(name)
            for name in (
                "torch",
                "transformers",
                "accelerate",
                "huggingface-hub",
                "PyYAML",
                "pandas",
                "matplotlib",
            )
        },
        "dependency_freeze_sha256": _file_sha256(freeze_path),
        "model_snapshot_fingerprint": snapshot_fingerprint,
        "cuda_runtime": torch.version.cuda,
        "driver": _command_output(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"]
        ),
        "gpu_names": [
            torch.cuda.get_device_name(index) for index in range(torch.cuda.device_count())
        ],
        "gpu_capabilities": [
            list(torch.cuda.get_device_capability(index))
            for index in range(torch.cuda.device_count())
        ],
        "gpu_count": torch.cuda.device_count(),
        "machine_id": machine_id,
        "gce_instance_id": gce_instance_id,
        "dtype": str(dtype).removeprefix("torch."),
        "model": str(model["id"]),
        "model_revision": str(model["revision"]),
    }


def runtime_fingerprint(runtime: Mapping[str, object]) -> str:
    return stable_fingerprint(runtime)


def source_contract(source: Mapping[str, object]) -> dict[str, object]:
    return {
        "git_commit": source["git_commit"],
        "git_dirty": source["git_dirty"],
        "dirty_state_fingerprint": source["dirty_state_fingerprint"],
        "critical_source_dirty": source.get("critical_source_dirty", False),
        "source_tree_fingerprint": source.get("source_tree_fingerprint"),
    }


def create_run_metadata(
    config: Mapping[str, object],
    source: Mapping[str, object],
    runtime: Mapping[str, object],
) -> dict[str, object]:
    cases = expected_cases(config)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(uuid.uuid4()),
        "started_at": utc_now(),
        "source": source_contract(source),
        "config_fingerprint": config_fingerprint(config),
        "runtime_fingerprint": runtime_fingerprint(runtime),
        "scientific_config": scientific_config(config),
        "runtime": dict(runtime),
        "expected_case_count": len(cases),
    }


def validate_run_metadata(
    metadata: Mapping[str, object],
    config: Mapping[str, object],
    source: Mapping[str, object] | None = None,
    runtime: Mapping[str, object] | None = None,
) -> None:
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported or missing Week 1 run metadata schema_version")
    try:
        uuid.UUID(str(metadata["run_id"]))
    except (KeyError, ValueError) as error:
        raise ValueError("Run metadata contains an invalid run_id") from error
    expected_fingerprint = config_fingerprint(config)
    if metadata.get("config_fingerprint") != expected_fingerprint:
        raise ValueError("Run metadata does not match the current scientific config")
    if metadata.get("scientific_config") != scientific_config(config):
        raise ValueError("Run metadata scientific_config is inconsistent")
    if metadata.get("expected_case_count") != len(expected_cases(config)):
        raise ValueError("Run metadata expected_case_count is inconsistent")
    if source is not None and metadata.get("source") != source_contract(source):
        raise ValueError("Cannot resume: Git source identity differs from the original run")
    if runtime is not None:
        fingerprint = runtime_fingerprint(runtime)
        if metadata.get("runtime_fingerprint") != fingerprint:
            raise ValueError("Cannot resume: GPU or software runtime identity changed")
        if metadata.get("runtime") != dict(runtime):
            raise ValueError("Run metadata runtime details are inconsistent")


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Run metadata {name} must be a mapping")
    return value


def _parse_nonnegative_float(row: Mapping[str, str], field: str, line: int) -> float:
    raw = row.get(field, "")
    if raw == "":
        raise ValueError(f"Row {line} has a blank required field {field}")
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"Row {line} has an invalid float in {field}: {raw!r}") from error
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Row {line} has a non-finite or negative {field}: {raw!r}")
    return value


def validate_result_rows(
    rows: list[dict[str, str]],
    metadata: Mapping[str, object],
    config: Mapping[str, object],
    *,
    require_complete: bool,
) -> set[tuple[int, int, int, bool]]:
    expected = expected_cases(config)
    seen: set[tuple[int, int, int, bool]] = set()
    output_hashes: dict[tuple[int, int, int], str] = {}
    source = _mapping(metadata.get("source"), "source")
    runtime = _mapping(metadata.get("runtime"), "runtime")
    expected_identity = {
        "run_id": str(metadata["run_id"]),
        "git_commit": str(source["git_commit"]),
        "config_fingerprint": str(metadata["config_fingerprint"]),
        "runtime_fingerprint": str(metadata["runtime_fingerprint"]),
        "model": str(runtime["model"]),
        "model_revision": str(runtime["model_revision"]),
        "dtype": str(runtime["dtype"]),
    }

    for line, row in enumerate(rows, start=2):
        for field in RESULT_FIELDS:
            if field not in row:
                raise ValueError(f"Row {line} is missing field {field}")
        for field in IDENTITY_FIELDS:
            if row[field] != expected_identity[field]:
                raise ValueError(
                    f"Row {line} identity mismatch for {field}: "
                    f"expected {expected_identity[field]!r}, got {row[field]!r}"
                )
        try:
            datetime.fromisoformat(row["timestamp"])
        except ValueError as error:
            raise ValueError(f"Row {line} has an invalid timestamp") from error
        try:
            integers = {field: int(row[field]) for field in INTEGER_FIELDS}
        except ValueError as error:
            raise ValueError(f"Row {line} has an invalid integer field") from error
        if (
            integers["repeat"] < 0
            or integers["prompt_tokens"] < 1
            or integers["output_tokens"] < 1
        ):
            raise ValueError(f"Row {line} has an out-of-range case key")
        parse_bool(row["use_cache"])
        key = case_key(row)
        if key in seen:
            raise ValueError(f"Duplicate result case at row {line}: {key}")
        if key not in expected:
            raise ValueError(f"Unexpected result case at row {line}: {key}")
        seen.add(key)

        measurements = {
            field: _parse_nonnegative_float(row, field, line) for field in FLOAT_FIELDS
        }
        if measurements["total_generation_ms"] <= 0:
            raise ValueError(f"Row {line} total_generation_ms must be positive")
        if measurements["output_tokens_per_second"] <= 0:
            raise ValueError(f"Row {line} output_tokens_per_second must be positive")

        if integers["output_tokens"] == 1:
            if any(row[field] != "" for field in TPOT_FIELDS):
                raise ValueError(f"Row {line} must leave TPOT fields blank for one output token")
        else:
            for field in TPOT_FIELDS:
                _parse_nonnegative_float(row, field, line)

        if not re.fullmatch(r"[0-9a-f]{16}", row["output_token_hash"]):
            raise ValueError(f"Row {line} has an invalid output_token_hash")
        output_key = (
            integers["prompt_tokens"],
            integers["output_tokens"],
            integers["repeat"],
        )
        previous_hash = output_hashes.setdefault(output_key, row["output_token_hash"])
        if previous_hash != row["output_token_hash"]:
            raise ValueError(f"Cache on/off output mismatch for case {output_key}")

        if not math.isclose(
            measurements["inference_ttft_ms"],
            measurements["h2d_ms"]
            + measurements["prefill_forward_ms"]
            + measurements["first_token_selection_ms"],
            rel_tol=1e-9,
            abs_tol=1e-6,
        ):
            raise ValueError(f"Row {line} has an inconsistent inference_ttft_ms")
        if not math.isclose(
            measurements["end_to_end_ttft_ms"],
            measurements["tokenization_ms"] + measurements["inference_ttft_ms"],
            rel_tol=1e-9,
            abs_tol=1e-6,
        ):
            raise ValueError(f"Row {line} has an inconsistent end_to_end_ttft_ms")
        if not math.isclose(
            measurements["total_generation_ms"],
            measurements["inference_ttft_ms"] + measurements["decode_ms"],
            rel_tol=1e-9,
            abs_tol=1e-6,
        ):
            raise ValueError(f"Row {line} has an inconsistent total_generation_ms")
        if not math.isclose(
            measurements["end_to_end_ms"],
            measurements["tokenization_ms"] + measurements["total_generation_ms"],
            rel_tol=1e-9,
            abs_tol=1e-6,
        ):
            raise ValueError(f"Row {line} has an inconsistent end_to_end_ms")
        expected_throughput = integers["output_tokens"] / (
            measurements["total_generation_ms"] / 1_000
        )
        if not math.isclose(
            measurements["output_tokens_per_second"],
            expected_throughput,
            rel_tol=1e-9,
            abs_tol=1e-6,
        ):
            raise ValueError(f"Row {line} has inconsistent output_tokens_per_second")

    missing = expected.difference(seen)
    if require_complete and missing:
        preview = sorted(missing)[:5]
        raise ValueError(
            f"Week 1 matrix is incomplete: {len(seen)}/{len(expected)} cases; "
            f"first missing cases: {preview}"
        )
    return seen
