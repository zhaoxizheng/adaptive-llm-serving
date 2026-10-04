"""Discover the actual exposition and persist bounded Prometheus range queries."""

from __future__ import annotations

import argparse
import json
import math
import re
import urllib.parse
import urllib.request

from src.common import load_yaml, read_json, write_json
from src.study_contract import VLLM_VERSION


def inventory(exposition):
    metrics = {}
    types = {}
    for line in exposition.splitlines():
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split()
            types[name] = kind
        elif line and not line.startswith("#"):
            match = re.match(r"([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*?)\})?\s+", line)
            if not match:
                raise ValueError("malformed Prometheus exposition")
            name, labels = match.groups()
            item = metrics.setdefault(name, {"labels": set()})
            item["labels"].update(re.findall(r'(\w+)="(?:[^"\\]|\\.)*"', labels or ""))
    for name, item in metrics.items():
        family = re.sub(r"_(bucket|sum|count|total|created)$", "", name)
        item.update(
            type=types.get(name, types.get(family, "untyped")), labels=sorted(item["labels"])
        )
    return metrics


def render_queries(profile, discovered, labels, scrape_seconds):
    if str(profile["vllm_version"]) != VLLM_VERSION:
        raise ValueError("query profile does not match pinned vLLM version")
    if not labels or not {"job", "instance"} <= labels.keys():
        raise ValueError("queries must select the exact Prometheus job and instance")
    selector = ",".join(f"{key}={json.dumps(str(value))}" for key, value in sorted(labels.items()))
    queries = {}
    for name, item in profile["queries"].items():
        if item["metric"] not in discovered:
            if item.get("required", True):
                raise ValueError(f"metric absent from this runtime: {item['metric']}")
            continue
        window = max(1, math.ceil(4 * scrape_seconds))
        expression = item["promql"].replace("{{labels}}", selector)
        expression = expression.replace("{window}", str(window))
        queries[name] = {
            **item,
            "expression": expression,
            "offset_seconds": window if item["kind"] == "histogram" else 0,
        }
    # Raw scrape timestamps detect a stale gauge repeated by Prometheus lookback.
    metric = profile["queries"]["waiting"]["metric"]
    queries["scrape_timestamp"] = {
        "expression": f"max(timestamp({metric}{{{selector}}}))",
        "kind": "timestamp",
        "unit": "seconds",
        "offset_seconds": 0,
    }
    return queries


def get_json(url):
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.load(response)


def capture(settings, metadata, exposition, *, fetch=get_json):
    start, end = metadata["measurement_start"], metadata["measurement_end"]
    if end <= start:
        raise ValueError("measurement window must be positive")
    profile = load_yaml(settings["queries"])
    discovered = inventory(exposition)
    result = {
        "run_id": metadata["run_id"],
        "start": start,
        "end": end,
        "step_seconds": settings["step_seconds"],
        "inventory": discovered,
        "queries": {},
        "errors": [],
    }
    try:
        queries = render_queries(
            profile, discovered, settings["labels"], settings["scrape_interval_seconds"]
        )
    except ValueError as error:
        result["errors"].append(str(error))
        return result
    for name, query in queries.items():
        item = dict(query)
        query_start = start + query["offset_seconds"]
        try:
            if query_start >= end:
                raise ValueError("measurement window is shorter than histogram rate window")
            params = urllib.parse.urlencode(
                {
                    "query": query["expression"],
                    "start": query_start,
                    "end": end,
                    "step": settings["step_seconds"],
                }
            )
            payload = fetch(
                settings["prometheus_url"].rstrip("/") + "/api/v1/query_range?" + params
            )
            item["response"] = payload
            if payload.get("status") != "success" or payload.get("warnings"):
                raise ValueError("Prometheus returned an error or warning")
            data = payload["data"]
            if data.get("resultType") != "matrix" or len(data.get("result", [])) != 1:
                raise ValueError("expected one nonempty, instance-scoped series")
            samples = data["result"][0]["values"]
            values = [(float(t), float(v)) for t, v in samples]
            if len(values) < 3 or any(not math.isfinite(v) for _, v in values):
                raise ValueError("missing/nonfinite samples")
            if any(
                b[0] <= a[0] or b[0] - a[0] > 1.5 * settings["step_seconds"]
                for a, b in zip(values, values[1:])
            ):
                raise ValueError("range-query sample gap or duplicate timestamp")
            tolerance = 1.5 * settings["step_seconds"]
            if values[0][0] > query_start + tolerance or values[-1][0] < end - tolerance:
                raise ValueError("range-query does not cover the measurement window")
            if query["kind"] == "counter" and any(b[1] < a[1] for a, b in zip(values, values[1:])):
                raise ValueError("counter reset during measurement")
            if name == "scrape_timestamp" and any(
                t - v > 2.5 * settings["scrape_interval_seconds"] or v > t + 1 for t, v in values
            ):
                raise ValueError("stale scrape or clock mismatch")
            item["values"] = values
        except (ValueError, KeyError, TypeError, OSError) as error:
            item["error"] = type(error).__name__ + ": " + str(error)[:200]
            if query.get("required", True):
                result["errors"].append(name + ": " + item["error"])
        result["queries"][name] = item
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/week05.yaml")
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    from pathlib import Path

    root = Path(args.run_dir)
    metadata = read_json(root / "metadata.json")
    data = capture(
        load_yaml(args.config)["observability"], metadata, (root / "metrics-before.txt").read_text()
    )
    write_json(root / "prometheus.json", data)
    if data["errors"]:
        raise SystemExit("metric capture is invalid; see prometheus.json")


if __name__ == "__main__":
    main()
