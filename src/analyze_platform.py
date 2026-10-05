"""Strict evidence analysis for EPP, cache identity, resource DAGs and scaling."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path

from src.common import write_json, write_text
from src.serving_experiment import percentile, summarize


def read_rows(path):
    rows = [
        json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()
    ]
    if any(not isinstance(r, dict) for r in rows):
        raise ValueError(
            "JSONL records must be objects; malformed evidence is not skipped"
        )
    return rows


def epp_analysis(
    clients,
    decisions,
    dispatches,
    *,
    failure_mode="FailClose",
    fault=False,
    complete_dispatch_window=False,
):
    ids = [r["request_id"] for r in clients]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("empty or duplicated client evidence")
    selected, actual = defaultdict(list), defaultdict(list)
    for row in decisions:
        selected[row["request_id"]].append(row)
    for row in dispatches:
        actual[row["request_id"]].append(row)
    rows = []
    for client in clients:
        rid = client["request_id"]
        ds, hops = selected[rid], actual[rid]
        status = "incomplete"
        if fault and failure_mode == "FailClose":
            if hops or client["status"] == "success":
                status = "unexpected_dispatch_or_success"
            elif complete_dispatch_window and client["status"] == "http_error":
                status = "closed"
        elif fault and failure_mode == "FailOpen":
            status = "fallback_observed" if hops else "incomplete"
        elif len(ds) == 1 and len(hops) == 1:
            status = (
                "matched"
                if ds[0].get("selected_pod_uid") == hops[0].get("pod_uid")
                else "mismatch"
            )
            legal = ds[0].get("ready_candidate_uids")
            if legal is None:
                status = "incomplete"
            elif ds[0].get("selected_pod_uid") not in legal:
                status = "illegal_candidate"
        elif len(hops) > 1 or len(ds) > 1:
            status = "multiple_attempts"
        rows.append(
            dict(
                request_id=rid,
                status=status,
                attempts=len(hops),
                client_status=client["status"],
                actual_pod_uids=[h.get("pod_uid") for h in hops],
            )
        )
    return dict(
        requests=len(rows),
        rows=rows,
        counts={
            s: sum(r["status"] == s for r in rows)
            for s in sorted({r["status"] for r in rows})
        },
        conformance="not_inferred_from_request_smoke",
    )


IDENTITY_FIELDS = (
    "model_revision",
    "tokenizer_revision",
    "template_revision",
    "adapter_id",
    "namespace",
    "pod_uid",
    "process_generation",
)


def prefix_analysis(decisions, reuse):
    """Do not infer actual KV reuse from affinity, predicted tokens or missing evidence."""
    engines = defaultdict(list)
    for row in reuse:
        engines[row["request_id"]].append(row)
    rows = []
    for decision in decisions:
        records = engines[decision["request_id"]]
        status, actual = "not_observed", None
        if len(records) == 1:
            engine = records[0]
            identity = decision.get("identity", {})
            other = engine.get("identity", {})
            if any(k not in identity or k not in other for k in IDENTITY_FIELDS):
                status = "incomplete_identity"
            elif identity != other or identity["pod_uid"] != decision.get(
                "selected_pod_uid"
            ):
                status = "identity_mismatch"
            elif (
                type(engine.get("reused_tokens")) is int
                and engine["reused_tokens"] >= 0
            ):
                status, actual = "observed", engine["reused_tokens"]
        elif len(records) > 1:
            status = "ambiguous_attempts"
        rows.append(
            dict(
                request_id=decision["request_id"],
                status=status,
                predicted_tokens=decision.get("predicted_tokens"),
                actual_reused_tokens=actual,
                load_age_seconds=decision.get("load_age_seconds"),
                routing_latency_ms=decision.get("routing_latency_ms"),
            )
        )
    return rows


def grouped_slo(clients, slo):
    if not clients:
        raise ValueError("empty client evidence")
    window = max(r["completed_at"] for r in clients) - min(
        r["scheduled_at"] for r in clients
    )
    groups = {"overall": clients}
    for field in ("workload", "prefix_family"):
        for value in {str(r.get(field, "unknown")) for r in clients}:
            groups[f"{field}:{value}"] = [
                r for r in clients if str(r.get(field, "unknown")) == value
            ]
    return {
        name: {
            **summarize(rows, slo, window),
            "ttft_p99_ms": percentile([r.get("ttft_ms") for r in rows], 0.99),
        }
        for name, rows in groups.items()
    }


def resource_graph(objects):
    nodes, edges, external = {}, [], set()
    for obj in objects:
        md = obj["metadata"]
        uid = md["uid"]
        if uid in nodes:
            raise ValueError("duplicate UID in resource snapshot")
        nodes[uid] = dict(
            uid=uid,
            kind=obj["kind"],
            name=md["name"],
            namespace=md.get("namespace"),
            generation=md.get("generation"),
            finalizers=md.get("finalizers", []),
            conditions=obj.get("status", {}).get("conditions", []),
            managers=[
                dict(
                    manager=f.get("manager"),
                    operation=f.get("operation"),
                    fields=f.get("fieldsV1", {}),
                )
                for f in md.get("managedFields", [])
            ],
        )
        for owner in md.get("ownerReferences", []):
            edges.append(
                dict(
                    owner_uid=owner["uid"],
                    child_uid=uid,
                    controller=owner.get("controller", False),
                )
            )
    external.update(e["owner_uid"] for e in edges if e["owner_uid"] not in nodes)
    return dict(
        nodes=list(nodes.values()), edges=edges, external_owner_uids=sorted(external)
    )


def deletion_audit(before, after, parent_uid):
    graph = resource_graph(before)
    descendants = {parent_uid}
    while True:
        expanded = descendants | {
            e["child_uid"] for e in graph["edges"] if e["owner_uid"] in descendants
        }
        if expanded == descendants:
            break
        descendants = expanded
    old, live = {o["metadata"]["uid"] for o in before}, {
        o["metadata"]["uid"] for o in after
    }
    if parent_uid not in old:
        raise ValueError("parent UID absent from before snapshot")
    return dict(
        remaining_owned_uids=sorted(descendants & live),
        removed_owned_uids=sorted(descendants - live),
        removed_unowned_uids=sorted((old - descendants) - live),
        limitation="compare equally scoped inventories; absence alone does not prove garbage-collector causality",
    )


def metric_value(response, now, max_age):
    if (
        response.get("status") != "success"
        or response.get("data", {}).get("resultType") != "vector"
    ):
        raise ValueError("metric query failed or did not return a vector")
    values = response["data"]["result"]
    if len(values) != 1:
        raise ValueError("metric must contain exactly one workload-total series")
    stamp, raw = values[0]["value"]
    value = float(raw)
    if not math.isfinite(value) or value < 0 or not 0 <= now - float(stamp) <= max_age:
        raise ValueError("metric is malformed, stale or from the future")
    return value


def allocation_cost(samples, end, *, max_gap, billed=None):
    if not samples or end < samples[-1]["timestamp"]:
        raise ValueError("missing allocation evidence or invalid end boundary")
    seconds = 0.0
    for i, row in enumerate(samples):
        stop = samples[i + 1]["timestamp"] if i + 1 < len(samples) else end
        duration = stop - row["timestamp"]
        value = row.get("allocated_gpus")
        if (
            duration < 0
            or duration > max_gap
            or value is None
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(
                "allocation sampling gap, malformed value or nonmonotonic time"
            )
        seconds += value * duration
    result = dict(
        allocated_gpu_hours=seconds / 3600,
        billed_gpu_hours=None,
        billed_node_hours=None,
        method="left-step integration of scheduled nonterminal target Pods",
    )
    if billed is not None:
        if (
            billed.get("start") != samples[0]["timestamp"]
            or billed.get("end") != end
            or not billed.get("source")
        ):
            raise ValueError(
                "billing evidence needs the same boundary and provider/lifecycle source"
            )
        for key in ("billed_gpu_hours", "billed_node_hours"):
            value = billed.get(key)
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("invalid billed quantity")
            result[key] = value
    return result


def cold_start(events):
    stages = (
        "observed",
        "desired",
        "scheduled",
        "model_ready",
        "route_eligible",
        "first_token",
    )
    result = []
    for row in events:
        stamps = [row.get(s) for s in stages]
        status = "complete"
        if any(s is None for s in stamps):
            status = "incomplete"
        elif any(b < a for a, b in zip(stamps, stamps[1:])):
            status = "nonmonotonic"
        result.append(
            dict(
                pod_uid=row["pod_uid"],
                status=status,
                durations_seconds=(
                    {f"{a}_to_{b}": row[b] - row[a] for a, b in zip(stages, stages[1:])}
                    if status == "complete"
                    else {}
                ),
            )
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=["epp", "prefix", "resources", "deletion", "scaling", "slo"]
    )
    parser.add_argument(
        "--input",
        required=True,
        help="JSONL clients, decisions, objects or allocation samples",
    )
    parser.add_argument(
        "--aux", help="JSONL dispatches, reuse, after objects or cold-start events"
    )
    parser.add_argument("--decisions", help="JSONL EPP decisions for epp mode")
    parser.add_argument("--parent-uid")
    parser.add_argument(
        "--failure-mode", choices=["FailClose", "FailOpen"], default="FailClose"
    )
    parser.add_argument("--fault", action="store_true")
    parser.add_argument("--complete-dispatch-window", action="store_true")
    parser.add_argument("--end", type=float)
    parser.add_argument("--max-gap", type=float, default=30)
    parser.add_argument(
        "--billing", help="provider/lifecycle JSON with start/end and source"
    )
    parser.add_argument("--baseline", default="configs/serving-platform-baseline.yaml")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rows, aux = read_rows(args.input), read_rows(args.aux) if args.aux else []
    if args.mode == "epp":
        result = epp_analysis(
            rows,
            read_rows(args.decisions) if args.decisions else [],
            aux,
            failure_mode=args.failure_mode,
            fault=args.fault,
            complete_dispatch_window=args.complete_dispatch_window,
        )
    elif args.mode == "prefix":
        result = prefix_analysis(rows, aux)
    elif args.mode == "resources":
        result = resource_graph(rows)
        # UID-derived identifiers avoid names being interpreted as Mermaid syntax.
        indexes = {n["uid"]: f"n{i}" for i, n in enumerate(result["nodes"])}
        lines = ["graph TD"]
        for n in result["nodes"]:
            lines.append(f'  {indexes[n["uid"]]}["{n["kind"]}/{n["name"]}"]')
        for e in result["edges"]:
            if e["owner_uid"] in indexes:
                lines.append(
                    f'  {indexes[e["owner_uid"]]} --> {indexes[e["child_uid"]]}'
                )
        write_text(Path(args.output).with_suffix(".mmd"), "\n".join(lines) + "\n")
    elif args.mode == "deletion":
        result = deletion_audit(rows, aux, args.parent_uid)
    elif args.mode == "slo":
        from src.common import load_yaml

        result = grouped_slo(rows, load_yaml(args.baseline)["slo"])
    else:
        if args.end is None:
            parser.error("scaling requires --end")
        result = allocation_cost(
            rows,
            args.end,
            max_gap=args.max_gap,
            billed=json.loads(Path(args.billing).read_text()) if args.billing else None,
        )
        result["cold_start"] = cold_start(aux)
    write_json(args.output, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
