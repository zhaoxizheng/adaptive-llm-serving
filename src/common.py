from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any


def load_yaml(path: str | Path) -> dict[str, Any]:
    import yaml

    with Path(path).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: str | Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def stable_fingerprint(payload: Any, length: int = 16) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(serialized.encode()).hexdigest()[:length]


def _git_output(arguments: list[str]) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *arguments],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _critical_source_files() -> dict[str, str]:
    files: dict[str, str] = {}
    for raw_path in sorted(_actual_critical_source_paths(Path.cwd())):
        path = Path(raw_path)
        if not path.is_file():
            raise RuntimeError(f"Unable to read source file: {raw_path}")
        files[raw_path] = _sha256_file(path)
    return files


def _actual_critical_source_paths(root: Path) -> set[str]:
    paths: set[str] = set()
    for name in ("Makefile", "pyproject.toml", "requirements.txt", "requirements-dev.txt"):
        if (root / name).is_file():
            paths.add(name)
    for directory_name in ("configs", "scripts", "src"):
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.name == ".DS_Store":
                continue
            paths.add(relative.as_posix())
    return paths


def source_identity(
    manifest_path: str | Path | None = None,
) -> dict[str, object]:
    commit = _git_output(["rev-parse", "HEAD"])
    if commit is not None:
        status = _git_output(
            [
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                ".",
                ":(exclude)results/**",
            ]
        )
        source_files = _critical_source_files()
        critical_changes = _git_output(
            [
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                "Makefile",
                "pyproject.toml",
                "requirements.txt",
                "requirements-dev.txt",
                "configs",
                "scripts",
                "src",
            ]
        )
        return {
            "git_commit": commit,
            "git_dirty": bool(status),
            "dirty_state_fingerprint": stable_fingerprint(status or "", length=64),
            "critical_source_dirty": bool(critical_changes),
            "source_tree_fingerprint": stable_fingerprint(source_files, length=64),
            "source_files": source_files,
            "source": "git",
        }

    source_manifest = Path(
        manifest_path
        or os.environ.get("EXPERIMENT_SOURCE_MANIFEST", ".experiment-source.json")
    )
    if not source_manifest.exists():
        raise RuntimeError(
            "Cannot identify the experiment source: no Git checkout or source manifest found."
        )
    payload = read_json(source_manifest)
    required = {
        "git_commit",
        "git_dirty",
        "dirty_state_fingerprint",
        "source_tree_fingerprint",
        "critical_source_dirty",
    }
    missing = required.difference(payload)
    if missing:
        raise RuntimeError(
            f"Source manifest {source_manifest} is missing fields: {sorted(missing)}"
        )
    source_files = payload.get("source_files")
    if not isinstance(source_files, dict) or not source_files:
        raise RuntimeError(f"Source manifest {source_manifest} has no file inventory")
    root = source_manifest.resolve().parent
    expected_paths = set(source_files)
    actual_paths = _actual_critical_source_paths(root)
    if actual_paths != expected_paths:
        missing_paths = sorted(expected_paths.difference(actual_paths))
        extra_paths = sorted(actual_paths.difference(expected_paths))
        raise RuntimeError(
            "Experiment source file set differs from the manifest; "
            f"missing={missing_paths}, extra={extra_paths}"
        )
    for relative_path, expected_hash in source_files.items():
        candidate = root / relative_path
        if not candidate.is_file():
            raise RuntimeError(f"Experiment source file is missing: {relative_path}")
        actual_hash = _sha256_file(candidate)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"Experiment source file differs from the manifest: {relative_path}"
            )
    fingerprint = stable_fingerprint(source_files, length=64)
    if payload.get("source_tree_fingerprint") != fingerprint:
        raise RuntimeError(f"Source manifest {source_manifest} has an invalid fingerprint")
    return {**payload, "source": "manifest"}


def require_clean_source(identity: dict[str, object]) -> None:
    commit = str(identity.get("git_commit", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError(f"Experiment source has an invalid Git commit: {commit!r}")
    if identity.get("critical_source_dirty", identity.get("git_dirty")) is not False:
        raise RuntimeError(
            "Official experiment runs require committed experiment code and config."
        )


def git_commit() -> str:
    try:
        return str(source_identity()["git_commit"])
    except RuntimeError:
        return "uncommitted"


def write_json(path: str | Path, payload: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_text(path: str | Path, content: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise

