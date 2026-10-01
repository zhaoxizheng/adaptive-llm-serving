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


def read_rows(
    path: str | Path, expected_fields: Sequence[str] | None = None
) -> list[dict[str, str]]:
    source = Path(path)
    if not source.exists() or source.stat().st_size == 0:
        return []
    with source.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return []
        duplicates = sorted({field for field in header if header.count(field) > 1})
        if duplicates:
            raise ValueError(f"CSV header contains duplicate fields: {duplicates}")
        if expected_fields is not None and header != list(expected_fields):
            raise ValueError(
                f"CSV header does not match the result schema: expected {list(expected_fields)}, "
                f"got {header}"
            )
        missing = set(CASE_KEY_FIELDS).difference(header)
        if missing:
            raise ValueError(f"Existing result file is missing fields: {sorted(missing)}")
        rows: list[dict[str, str]] = []
        for line_number, values in enumerate(reader, start=2):
            if len(values) != len(header):
                raise ValueError(
                    f"Malformed CSV row {line_number}: expected {len(header)} fields, "
                    f"got {len(values)}"
                )
            rows.append(dict(zip(header, values)))
        return rows


def append_row(path: str | Path, row: Mapping[str, object], fieldnames: Sequence[str]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if set(row) != set(fieldnames):
        missing = sorted(set(fieldnames).difference(row))
        extra = sorted(set(row).difference(fieldnames))
        raise ValueError(f"Result row schema mismatch; missing={missing}, extra={extra}")
    existing_rows = read_rows(destination, expected_fields=fieldnames)
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
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
