"""Read Nsight Systems SQLite exports; keep unknown gap attribution explicit."""

from __future__ import annotations

import argparse
import html
import sqlite3
from pathlib import Path

from src.common import write_json, write_text
from src.profile_tables import union_intervals, write_csv


def read_timeline(path):
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        strings = (
            dict(connection.execute("SELECT id, value FROM StringIds"))
            if "StringIds" in tables
            else {}
        )
        kinds = {
            "kernel": ("CUPTI_ACTIVITY_KIND_KERNEL", "CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL"),
            "api": ("CUPTI_ACTIVITY_KIND_RUNTIME",),
            "memcpy": ("CUPTI_ACTIVITY_KIND_MEMCPY",),
            "memset": ("CUPTI_ACTIVITY_KIND_MEMSET",),
            "nvtx": ("NVTX_EVENTS",),
        }
        result = {kind: [] for kind in kinds}
        for kind, choices in kinds.items():
            table = next((t for t in choices if t in tables), None)
            if table is None:
                continue
            # Table names come only from the constant allowlist above.
            for raw in connection.execute(f'SELECT * FROM "{table}"'):
                row = dict(raw)
                start, end = row.get("start"), row.get("end")
                if start is None or end is None:
                    continue  # Instant NVTX marks do not define an interval.
                if end < start:
                    raise ValueError("negative Nsight interval")
                name = row.get("text") or next(
                    (
                        strings.get(row[k])
                        for k in ("demangledName", "shortName", "nameId", "textId")
                        if row.get(k) is not None and row[k] in strings
                    ),
                    None,
                )
                result[kind].append(
                    dict(
                        start_ns=start,
                        end_ns=end,
                        duration_ns=end - start,
                        name=name or kind,
                        device=row.get("deviceId"),
                        stream=row.get("streamId"),
                        global_tid=row.get("globalTid"),
                        correlation_id=row.get("correlationId"),
                        copy_kind=row.get("copyKind"),
                        bytes=row.get("bytes"),
                        src_kind=row.get("srcKind"),
                        dst_kind=row.get("dstKind"),
                    )
                )
    if not result["kernel"] or not result["api"]:
        raise ValueError("SQLite export is missing CUDA kernel/API evidence")
    return result


def summarize(timeline):
    devices = {
        r["device"]
        for kind in ("kernel", "memcpy", "memset")
        for r in timeline[kind]
        if r["device"] is not None
    }
    if len(devices) > 1:
        raise ValueError("Week 12 busy/idle analysis requires one GPU")
    ranges = [r for r in timeline["nvtx"] if r["name"].startswith("study/execute/step=")]
    if not ranges:
        raise ValueError("no captured engine-step NVTX ranges")
    start, end = min(r["start_ns"] for r in ranges), max(r["end_ns"] for r in ranges)
    if end <= start:
        raise ValueError("empty capture window")
    intervals = [
        (max(start, r["start_ns"]), min(end, r["end_ns"]))
        for kind in ("kernel", "memcpy", "memset")
        for r in timeline[kind]
        if r["end_ns"] > start and r["start_ns"] < end
    ]
    busy = union_intervals(intervals)
    gaps, cursor = [], start
    for left, right in busy + [[end, end]]:
        if left > cursor:
            evidence = {
                kind: sorted(
                    {
                        r["name"]
                        for r in timeline[kind]
                        if r["start_ns"] < left and r["end_ns"] > cursor
                    }
                )
                for kind in ("api", "nvtx")
            }
            gaps.append(
                dict(
                    start_ns=cursor,
                    end_ns=left,
                    duration_ns=left - cursor,
                    classification="unknown",
                    overlapping_evidence=evidence,
                )
            )
        cursor = max(cursor, right)
    occupied = sum(b - a for a, b in busy)
    return dict(
        window_start_ns=start,
        window_end_ns=end,
        gpu_busy_ns=occupied,
        gpu_idle_ns=end - start - occupied,
        busy_fraction=occupied / (end - start),
        gaps=gaps,
        clock="Nsight export nanoseconds; union of kernel/memcpy/memset intervals",
        attribution="overlap lists are candidates; CPU causality and queue state need review",
        timeline=timeline,
    )


def draw_svg(result, path):
    start, end = result["window_start_ns"], result["window_end_ns"]
    scale = 1050 / (end - start)
    elements = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="360">',
        '<rect width="1280" height="360" fill="white"/>',
        '<text x="20" y="28">Captured CPU/GPU timeline (Nsight clock, ms relative to first step)</text>',
    ]
    for index, (kind, color) in enumerate(
        (
            ("nvtx", "#7464aa"),
            ("api", "#e2a23b"),
            ("kernel", "#2887ba"),
            ("memcpy", "#389574"),
            ("memset", "#6aa570"),
        )
    ):
        y = 60 + index * 48
        elements.append(f'<text x="20" y="{y+15}">{kind}</text>')
        for row in result["timeline"][kind]:
            left, right = max(start, row["start_ns"]), min(end, row["end_ns"])
            if right <= left:
                continue
            title = html.escape(f'{row["name"]}: {(right-left)/1000:.3f} us')
            elements.append(
                f'<rect x="{160+(left-start)*scale:.3f}" y="{y}" '
                f'width="{max(.4, (right-left)*scale):.3f}" height="20" '
                f'fill="{color}" opacity="0.65"><title>{title}</title></rect>'
            )
    for tick in range(6):
        elements.append(f'<text x="{160+tick*210}" y="320">{(end-start)*tick/5e6:.2f}</text>')
    elements.append(
        '<text x="20" y="350">GPU busy uses interval union. Gaps remain unknown until evidence is reviewed.</text></svg>'
    )
    write_text(path, "\n".join(elements))


def write_summary(sqlite_path, output, figure=None):
    result = summarize(read_timeline(sqlite_path))
    root = Path(output)
    for kind, rows in result["timeline"].items():
        write_csv(root / f"{kind}.csv", rows)
    write_csv(root / "gaps.csv", result["gaps"])
    write_json(root / "summary.json", result)
    if figure:
        draw_svg(result, figure)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--figure")
    args = parser.parse_args()
    write_summary(args.sqlite, args.output, args.figure)


if __name__ == "__main__":
    main()
