from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from huggingface_hub import snapshot_download

from src.common import load_yaml, utc_now, write_json
from src.week01_contract import PINNED_REVISION_PATTERN, model_snapshot_fingerprint


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download and inventory the immutable Week 1 model snapshot."
    )
    parser.add_argument("--config", default="configs/week01.yaml")
    parser.add_argument("--output")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    args = parse_args()
    config = load_yaml(args.config)
    output_path = args.output or config["output"]["model_snapshot"]
    model = config["model"]
    revision = str(model["revision"])
    if not PINNED_REVISION_PATTERN.fullmatch(revision):
        raise ValueError("model.revision must be an immutable Hugging Face commit SHA")
    snapshot = Path(snapshot_download(repo_id=model["id"], revision=revision))
    snapshot_revision = snapshot.name
    if snapshot_revision != revision:
        raise RuntimeError(
            f"Downloaded snapshot resolved to {snapshot_revision}, expected {revision}"
        )
    files = []
    for path in sorted(item for item in snapshot.rglob("*") if item.is_file()):
        files.append(
            {
                "path": str(path.relative_to(snapshot)),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    payload = {
        "captured_at": utc_now(),
        "model": model["id"],
        "requested_revision": revision,
        "resolved_revision": snapshot_revision,
        "snapshot_path": str(snapshot),
        "files": files,
    }
    payload["snapshot_fingerprint"] = model_snapshot_fingerprint(payload)
    write_json(output_path, payload)
    print(f"Prepared {model['id']} at {snapshot_revision}; inventoried {len(files)} files.")


if __name__ == "__main__":
    main()
