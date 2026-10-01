from __future__ import annotations

import hashlib
import json

import pytest

import src.common as common


def test_non_git_source_manifest_validates_file_inventory(tmp_path, monkeypatch) -> None:
    source_file = tmp_path / "src" / "example.py"
    source_file.parent.mkdir()
    source_file.write_text("answer = 42\n", encoding="utf-8")
    digest = hashlib.sha256(source_file.read_bytes()).hexdigest()
    source_files = {"src/example.py": digest}
    manifest = tmp_path / ".experiment-source.json"
    manifest.write_text(
        json.dumps(
            {
                "git_commit": "a" * 40,
                "git_dirty": False,
                "critical_source_dirty": False,
                "dirty_state_fingerprint": "b" * 64,
                "source_tree_fingerprint": common.stable_fingerprint(
                    source_files, length=64
                ),
                "source_files": source_files,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(common, "_git_output", lambda _arguments: None)

    identity = common.source_identity(manifest)

    assert identity["git_commit"] == "a" * 40
    assert identity["source"] == "manifest"


def test_non_git_source_manifest_rejects_modified_file(tmp_path, monkeypatch) -> None:
    source_file = tmp_path / "src" / "example.py"
    source_file.parent.mkdir()
    source_file.write_text("answer = 42\n", encoding="utf-8")
    source_files = {
        "src/example.py": hashlib.sha256(source_file.read_bytes()).hexdigest()
    }
    manifest = tmp_path / ".experiment-source.json"
    manifest.write_text(
        json.dumps(
            {
                "git_commit": "a" * 40,
                "git_dirty": False,
                "critical_source_dirty": False,
                "dirty_state_fingerprint": "b" * 64,
                "source_tree_fingerprint": common.stable_fingerprint(
                    source_files, length=64
                ),
                "source_files": source_files,
            }
        ),
        encoding="utf-8",
    )
    source_file.write_text("answer = 43\n", encoding="utf-8")
    monkeypatch.setattr(common, "_git_output", lambda _arguments: None)

    with pytest.raises(RuntimeError, match="differs from the manifest"):
        common.source_identity(manifest)


def test_non_git_source_manifest_rejects_unlisted_executable(tmp_path, monkeypatch) -> None:
    source_file = tmp_path / "src" / "example.py"
    source_file.parent.mkdir()
    source_file.write_text("answer = 42\n", encoding="utf-8")
    source_files = {
        "src/example.py": hashlib.sha256(source_file.read_bytes()).hexdigest()
    }
    manifest = tmp_path / ".experiment-source.json"
    manifest.write_text(
        json.dumps(
            {
                "git_commit": "a" * 40,
                "git_dirty": False,
                "critical_source_dirty": False,
                "dirty_state_fingerprint": "b" * 64,
                "source_tree_fingerprint": common.stable_fingerprint(
                    source_files, length=64
                ),
                "source_files": source_files,
            }
        ),
        encoding="utf-8",
    )
    (source_file.parent / "stale.py").write_text("raise SystemExit\n", encoding="utf-8")
    monkeypatch.setattr(common, "_git_output", lambda _arguments: None)

    with pytest.raises(RuntimeError, match="extra=.*stale.py"):
        common.source_identity(manifest)
