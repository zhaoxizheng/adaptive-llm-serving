from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

CASE_KEY_FIELDS = ("prompt_tokens", "output_tokens", "repeat", "use_cache")


def parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def case_key(row: Mapping[str, object]) -> tuple[int, int, int, bool]:
    return (
        int(row["prompt_tokens"]),
        int(row["output_tokens"]),
        int(row["repeat"]),
        parse_bool(row["use_cache"]),
    )


def read_rows(path: str | Path) -> list[dict[str, str]]:
    source = Path(path)
    if not source.exists() or source.stat().st_size == 0:
        return []
    with source.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = set(CASE_KEY_FIELDS).difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Existing result file is missing fields: {sorted(missing)}")
        return list(reader)


def append_row(path: str | Path, row: Mapping[str, object], fieldnames: Sequence[str]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing_rows = read_rows(destination)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(existing_rows)
            writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
