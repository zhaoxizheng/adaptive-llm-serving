from __future__ import annotations

import argparse
from collections.abc import Mapping

from src.common import load_yaml


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read one scalar value from a YAML config.")
    parser.add_argument("--config", default="configs/week01.yaml")
    parser.add_argument("--key", required=True, help="Dot-separated key, such as output.run_log")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    value: object = load_yaml(args.config)
    for part in args.key.split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise KeyError(f"Config key does not exist: {args.key}")
        value = value[part]
    if isinstance(value, (Mapping, list)) or value is None:
        raise ValueError(f"Config key is not a scalar: {args.key}")
    print(value)


if __name__ == "__main__":
    main()
