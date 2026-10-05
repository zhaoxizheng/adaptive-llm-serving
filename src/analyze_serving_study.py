"""Offline per-repeat tables and TTFT plots; never select a baseline from profiler runs."""

import argparse
import html
import json
from pathlib import Path

from src.common import write_json, write_text
from src.profile_tables import write_csv
from src.serving_experiment import counter_delta


def analyze(root, output):
    rows = []
    for filename in sorted(Path(root).rglob("summary.json")):
        run_file = filename.parent / "run.json"
        if not run_file.is_file():
            continue
        meta, summary = json.loads(run_file.read_text()), json.loads(filename.read_text())
        if meta.get("profiler"):
            raise ValueError("counter/profile runs cannot enter service comparisons")
        row = dict(run=str(filename.parent), case=meta.get("case"), repeat=meta.get("repeat"),
                   workload=meta.get("workload"), workload_id=meta.get("workload_id"), status=meta["status"],
                   **{k: v for k, v in summary.items() if not isinstance(v, dict)})
        rows.append(row)
        client_file = filename.parent / "client.jsonl"
        clients = [json.loads(line) for line in client_file.read_text().splitlines()] if client_file.exists() else []
        clients.sort(key=lambda r: r["scheduled_at"])
        values = [(i, r["ttft_ms"]) for i, r in enumerate(clients) if r.get("ttft_ms") is not None]
        maximum = max([v for _, v in values] or [1]) or 1
        points = " ".join(f"{40 + i * 710 / max(len(clients) - 1, 1):.1f},{260 - v * 220 / maximum:.1f}" for i, v in values)
        title = html.escape(f"{meta.get('case')} repeat={meta.get('repeat')} TTFT by arrival order")
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="310"><rect width="100%" height="100%" fill="white"/><text x="40" y="20">{title}</text><text x="40" y="40">max={maximum:.1f} ms</text><polyline fill="none" stroke="#2563eb" points="{points}"/><text x="40" y="295">Arrival index; missing/failed requests remain in raw data</text></svg>'
        write_text(Path(output) / f"ttft-{len(rows):03d}.svg", svg)
        series_file = filename.parent / "metric-series.jsonl"
        metric = meta.get("config", {}).get("cache_metrics")
        if series_file.is_file() and metric:
            series = [json.loads(line) for line in series_file.read_text().splitlines()]
            samples = [s for s in series if "exposition" in s]
            cache_rows = []
            for before, after in zip(samples, samples[1:]):
                hits = counter_delta(before["exposition"], after["exposition"], metric["hits"])
                queries = counter_delta(before["exposition"], after["exposition"], metric["queries"])
                cache_rows.append(dict(start=before["timestamp"], end=after["timestamp"],
                    hit_delta=hits["value"], query_delta=queries["value"], hit_status=hits["status"],
                    query_status=queries["status"], configured_unit=metric["unit"],
                    definition_verified=bool(metric["definition"])))
            write_csv(Path(output) / f"cache-{len(rows):03d}.csv", cache_rows)
            values = [(i, r["hit_delta"]) for i, r in enumerate(cache_rows) if r["hit_delta"] is not None]
            maximum = max([v for _, v in values] or [1]) or 1
            points = " ".join(f"{40 + i * 710 / max(len(cache_rows) - 1, 1):.1f},{260 - v * 220 / maximum:.1f}" for i, v in values)
            label = "definition supplied" if metric["definition"] else "counter unit unverified"
            write_text(Path(output) / f"cache-{len(rows):03d}.svg",
                f'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="310"><rect width="100%" height="100%" fill="white"/><text x="40" y="20">Prefix hit deltas per sample interval; {label}</text><text x="40" y="40">max delta={maximum:.1f}</text><polyline fill="none" stroke="#059669" points="{points}"/><text x="40" y="295">Sample interval index; see CSV for actual timestamps/gaps</text></svg>')
    if not rows:
        raise ValueError("no service run summaries")
    write_csv(Path(output) / "runs.csv", rows)
    write_json(Path(output) / "analysis.json", dict(runs=rows,
        selection="manual evidence review required: quality, SLO, topology, KV capacity and repeat variance"))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    analyze(args.root, args.output)


if __name__ == "__main__":
    main()
