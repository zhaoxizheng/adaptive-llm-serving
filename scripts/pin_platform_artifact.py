"""Bind a reviewed local release artifact to a versions lock; does not freeze compatibility."""

import argparse
import hashlib
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", default="deploy/platform-versions.lock.yaml")
    parser.add_argument("--key", required=True)
    parser.add_argument("--path", required=True)
    args = parser.parse_args()
    path = Path(args.path)
    data = path.read_bytes()
    lock = yaml.safe_load(Path(args.lock).read_text())
    lock.setdefault("artifacts", {})[args.key] = dict(
        path=str(path), sha256=hashlib.sha256(data).hexdigest()
    )
    lock["frozen"] = False
    Path(args.lock).write_text(yaml.safe_dump(lock, sort_keys=False))
    print(f"Pinned {args.key}; review compatibility before setting frozen: true")


if __name__ == "__main__":
    main()
