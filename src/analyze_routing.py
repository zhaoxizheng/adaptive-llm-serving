"""Join client and upstream evidence; separate client requests from retry attempts."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from src.cluster_contract import weight_verdict
from src.common import write_json
from src.profile_tables import write_csv


def json_lines(paths):
    rows = []
    for filename in paths:
        for line in Path(filename).read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def rr_verdict(rows):
    groups = defaultdict(list)
    for row in rows:
        if row.get("event") == "request":
            groups[(row["gateway_id"], row["epoch"])].append(row)
    errors = []
    for group, requests in groups.items():
        requests.sort(key=lambda r: r["sequence"])
        pool = requests[0]["ready_uids"]
        previous = None
        counts = Counter()
        for row in requests:
            if row["ready_uids"] != pool or row["pod_uid"] not in pool:
                errors.append(f"{group}: inconsistent endpoint epoch")
                continue
            if previous:
                if row["sequence"] != previous["sequence"] + 1:
                    errors.append(f"{group}: missing/duplicated admission sequence")
                expected = pool[(pool.index(previous["pod_uid"]) + 1) % len(pool)]
                if row["pod_uid"] != expected:
                    errors.append(f"{group}: not request-level round robin")
            counts[row["pod_uid"]] += 1
            if max(counts[e] for e in pool) - min(counts[e] for e in pool) > 1:
                errors.append(f"{group}: admission counts differ by more than one")
            previous = row
    return dict(status="passed" if groups and not errors else "failed", epochs=len(groups), errors=errors)


def attribution(clients, attempts):
    ids = {r["request_id"] for r in clients}
    grouped = defaultdict(list)
    for attempt in attempts:
        if attempt.get("event") == "request" and attempt.get("request_id") in ids:
            grouped[attempt["request_id"]].append(attempt)
    rows = []
    for client in clients:
        evidence = grouped[client["request_id"]]
        rows.append(dict(request_id=client["request_id"], client_status=client["status"],
                         upstream_attempts=len(evidence),
                         pod_uids=sorted({e["pod_uid"] for e in evidence}),
                         backends=sorted({e["upstream"] for e in evidence}),
                         route_names=sorted({e["route_name"] for e in evidence if e.get("route_name")}),
                         statuses=[e.get("status") for e in evidence],
                         ttft_ms=client.get("ttft_ms"), output_tokens=client["output_tokens"]))
    return rows


def gpu_seconds(samples, end):
    """Integrate scheduled nonterminal Pod GPU allocations, not HPA desired counts."""
    if not samples or end < samples[-1]["timestamp"]:
        raise ValueError("invalid allocation window")
    total = 0
    for i, sample in enumerate(samples):
        stop = samples[i + 1]["timestamp"] if i + 1 < len(samples) else end
        if stop < sample["timestamp"] or "items" not in sample["pods"]:
            raise ValueError("missing/unordered Pod allocation evidence")
        count = 0
        for pod in sample["pods"]["items"]:
            if (pod["metadata"].get("labels", {}).get("serving-study-role") == "engine"
                    and pod["spec"].get("nodeName") and pod.get("status", {}).get("phase") not in {"Failed", "Succeeded"}):
                count += sum(int(c.get("resources", {}).get("limits", {}).get("nvidia.com/gpu", 0)) for c in pod["spec"]["containers"])
        total += count * (stop - sample["timestamp"])
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", nargs="+", required=True)
    parser.add_argument("--attempts", nargs="+", required=True, help="one telemetry layer only; never concatenate RR and identity logs")
    parser.add_argument("--output", required=True)
    parser.add_argument("--rr", action="store_true")
    parser.add_argument("--weights", nargs=2, type=int)
    parser.add_argument("--minimum-samples", type=int, default=1000)
    args = parser.parse_args()
    clients, attempts = json_lines(args.clients), json_lines(args.attempts)
    if not clients or len({r["request_id"] for r in clients}) != len(clients):
        raise ValueError("missing or duplicate client evidence")
    rows = attribution(clients, attempts)
    result = dict(client_requests=len(rows), upstream_attempts=sum(r["upstream_attempts"] for r in rows),
                  missing_attribution=sum(r["upstream_attempts"] == 0 for r in rows),
                  multiple_attempts=sum(r["upstream_attempts"] > 1 for r in rows),
                  route_attribution_missing=sum(not r["route_names"] for r in rows))
    if args.rr:
        selected = {r["request_id"] for r in clients}
        result["round_robin"] = rr_verdict([r for r in attempts if r.get("request_id") in selected])
    if args.weights:
        counts = Counter()
        for row in rows:
            if row["upstream_attempts"] != 1 or len(row["backends"]) != 1:
                continue
            name = row["backends"][0]
            backend = "a" if name == "vllm-a" or name.startswith("vllm-a-") else "b" if name == "vllm-b" or name.startswith("vllm-b-") else None
            if backend:
                counts[backend] += 1
        result["weights"] = (weight_verdict(counts["a"], counts["b"], args.weights, minimum=args.minimum_samples)
                             if sum(counts.values()) == len(rows) else {"status": "incomplete_or_retried_attribution"})
        result["backend_requests"] = dict(counts)
    root = Path(args.output)
    write_csv(root / "attribution.csv", rows)
    write_json(root / "routing.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
