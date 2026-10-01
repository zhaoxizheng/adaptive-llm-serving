from __future__ import annotations

import argparse
from pathlib import Path

from src.common import load_yaml, read_json, source_identity, utc_now, write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write the Week 1 runner status atomically."
    )
    parser.add_argument("--config", default="configs/week01.yaml")
    parser.add_argument(
        "--status",
        choices=("running", "artifacts_ready", "completed", "failed"),
        required=True,
    )
    parser.add_argument("--exit-code", type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_yaml(args.config)
    metadata_path = Path(config["output"]["run_metadata"])
    run_id = None
    if metadata_path.is_file():
        run_id = read_json(metadata_path).get("run_id")
    payload = {
        "schema_version": 1,
        "updated_at": utc_now(),
        "status": args.status,
        "exit_code": args.exit_code,
        "run_id": run_id,
        "source": source_identity(),
    }
    write_json(config["output"]["run_status"], payload)


if __name__ == "__main__":
    main()
