from __future__ import annotations

import argparse

from src.common import require_clean_source, source_identity, write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write the clean Git identity shipped to a non-Git experiment host."
    )
    parser.add_argument("--output", default=".experiment-source.json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    identity = source_identity()
    require_clean_source(identity)
    payload = {key: value for key, value in identity.items() if key != "source"}
    write_json(args.output, payload)
    print(f"Wrote source manifest for {payload['git_commit']} to {args.output}")


if __name__ == "__main__":
    main()
