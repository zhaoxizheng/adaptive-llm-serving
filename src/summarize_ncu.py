"""Read Nsight Compute long CSV without losing launch identity or metric units."""

import argparse
import csv
import io
import math
from pathlib import Path

from src.common import write_json
from src.profile_tables import write_csv


def parse_counters(text):
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if "Metric Name" in line and "Metric Value" in line), None)
    if start is None:
        raise ValueError("no Nsight Compute long CSV header; inspect export/permission logs")
    rows = []
    for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        if not row.get("Metric Name"):
            continue
        raw = row["Metric Value"]
        try:
            numeric = float(raw.replace(",", ""))
            numeric = numeric if math.isfinite(numeric) else None
        except (ValueError, AttributeError):
            numeric = None
        rows.append(dict(launch_id=row.get("ID"), process_id=row.get("Process ID"),
                         kernel=row.get("Kernel Name"), device=row.get("Device"),
                         context=row.get("Context"), stream=row.get("Stream"),
                         block_size=row.get("Block Size"), grid_size=row.get("Grid Size"),
                         section=row.get("Section Name"), metric=row["Metric Name"],
                         unit=row.get("Metric Unit"), value=numeric, raw_value=raw))
    if not rows:
        raise ValueError("empty counter export")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rows = parse_counters(Path(args.csv).read_text())
    root = Path(args.output)
    write_csv(root / "counters.csv", rows)
    write_json(root / "summary.json", dict(rows=len(rows), launches=sorted({str(r["launch_id"]) for r in rows}),
        verdict="requires hypothesis review; occupancy alone is not a bottleneck verdict"))


if __name__ == "__main__":
    main()
