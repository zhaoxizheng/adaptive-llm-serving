"""Rebuild SLO goodput and measured capacity brackets from raw run evidence."""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path

from src.common import read_json, write_json
from src.metrics import percentile
from src.study_contract import fingerprint


def slo_attained(record, slo):
    return record.get("status") == "success" and all(
        isinstance(record.get(key), (int, float))
        and math.isfinite(record[key])
        and 0 <= record[key] <= threshold
        for key, threshold in slo.items()
    )


def queue_growth(values, start, end):
    latter = [(t, v) for t, v in values if start + (end - start) / 2 <= t <= end]
    if len(latter) < 3:
        raise ValueError("waiting queue needs at least three samples in the second half")
    x = sum(t for t, _ in latter) / len(latter)
    y = sum(v for _, v in latter) / len(latter)
    variance = sum((t - x) ** 2 for t, _ in latter)
    if variance == 0:
        raise ValueError("duplicate queue timestamps")
    return sum((t - x) * (v - y) for t, v in latter) / variance


def rebuild_latencies(record):
    """Derive SLO inputs from event timestamps, never trust a summary in isolation."""
    scheduled = record["scheduled_at"]
    completed = record["completed_at"]
    first, last = record.get("first_content_at"), record.get("last_content_at")
    if not all(isinstance(t, (int, float)) and math.isfinite(t) for t in (scheduled, completed)):
        raise ValueError("nonfinite request timestamps")
    if completed < scheduled:
        raise ValueError("request finished before its scheduled arrival")
    result = {"e2e_ms": (completed - scheduled) * 1000, "ttft_ms": None, "tpot_ms": None}
    if first is not None:
        if not all(isinstance(t, (int, float)) and math.isfinite(t) for t in (first, last)):
            raise ValueError("invalid content timestamps")
        if not scheduled <= first <= last <= completed:
            raise ValueError("content timestamps violate request ordering")
        result["ttft_ms"] = (first - scheduled) * 1000
        if record.get("output_tokens", 0) > 1:
            result["tpot_ms"] = (last - first) * 1000 / (record["output_tokens"] - 1)
    if record["status"] == "success":
        if any(v is None for v in result.values()):
            raise ValueError("successful request lacks latency evidence")
        usage = record.get("usage") or {}
        if (
            usage.get("prompt_tokens") != record["prompt_tokens"]
            or usage.get("completion_tokens") != record["output_tokens"]
        ):
            raise ValueError("successful request token usage mismatch")
    return result


def analyze_run(root):
    root = Path(root)
    metadata = read_json(root / "metadata.json")
    summary = {
        "run_id": metadata["run_id"],
        "path": str(root),
        "valid": False,
        "stable": False,
        "invalid_reasons": [],
        "unstable_reasons": [],
        "mixture": metadata["mixture"],
        "offered_rps": metadata["offered_rps"],
        "repeat": metadata["repeat"],
        "tags": metadata.get("tags", {}),
    }
    invalid, unstable = summary["invalid_reasons"], summary["unstable_reasons"]
    if metadata["status"] != "completed":
        invalid.append("run did not complete")
        return summary
    config = metadata["config"]
    if fingerprint(config) != metadata["config_fingerprint"]:
        invalid.append("configuration fingerprint mismatch")
    records = read_json(root / "client.json")
    for row in records:
        if row["status"] == "client_overflow":
            continue
        try:
            rebuilt = rebuild_latencies(row)
            for key, value in rebuilt.items():
                if (
                    value is not None
                    and row.get(key) is not None
                    and not math.isclose(value, row[key], abs_tol=0.02, rel_tol=1e-5)
                ):
                    invalid.append(f"stored latency disagrees with timestamps: {key}")
            row.update(rebuilt)
        except (ValueError, KeyError, TypeError) as error:
            invalid.append("request timing/usage evidence invalid: " + str(error))
    start, end = metadata["measurement_start"], metadata["measurement_end"]
    seconds = end - start
    if seconds <= 0 or not records:
        raise ValueError("empty cohort or nonpositive measurement window")
    if len({r["request_id"] for r in records}) != len(records):
        invalid.append("duplicate request IDs")
    if any(not start <= r["scheduled_at"] < end for r in records):
        invalid.append("request outside measurement cohort")
    if any(r["status"] == "client_overflow" for r in records):
        invalid.append("client inflight limit reached")
    if any(r.get("arrival_lag_ms", 0) > config["capacity"]["max_arrival_lag_ms"] for r in records):
        invalid.append("client arrival lag exceeded contract")
    if metadata.get("gpu_errors"):
        invalid.append("GPU sampling failed")
    if not (root / "gpu.csv").is_file() or not (root / "server.log").is_file():
        invalid.append("GPU or server-log evidence missing")
    else:
        with (root / "gpu.csv").open() as handle:
            gpu = [r for r in csv.DictReader(handle) if start <= float(r["timestamp"]) <= end]
        if len(gpu) < 3:
            invalid.append("insufficient GPU samples")
        else:
            times = [float(r["timestamp"]) for r in gpu]
            tolerance = max(5, 3 * config["observability"]["gpu_interval_seconds"])
            if (
                times[0] - start > tolerance
                or end - times[-1] > tolerance
                or any(b - a > tolerance for a, b in zip(times, times[1:]))
            ):
                invalid.append("GPU sample gap")
            summary["gpu_memory_peak_mib"] = max(float(r["memory_used_mib"]) for r in gpu)
            summary["gpu_utilization_mean_pct"] = sum(
                float(r["utilization_pct"]) for r in gpu
            ) / len(gpu)
    successes = [r for r in records if r["status"] == "success"]
    complete_in_window = [r for r in successes if start <= r["completed_at"] <= end]
    attained = [r for r in records if slo_attained(r, config["slos"][r["workload"]])]
    good_in_window = [r for r in attained if r["completed_at"] <= end]
    summary.update(
        request_count=len(records),
        achieved_rps=len(complete_in_window) / seconds,
        scheduled_rps=len(records) / seconds,
        goodput_rps=len(attained) / seconds,
        completed_goodput_rps=len(good_in_window) / seconds,
        slo_attainment_fraction=len(attained) / len(records),
        error_rate=1 - len(successes) / len(records),
        timeout_rate=sum(r["status"] == "timeout" for r in records) / len(records),
        input_tokens_per_second=sum(r["prompt_tokens"] for r in complete_in_window) / seconds,
        output_tokens_per_second=sum(r["output_tokens"] for r in complete_in_window) / seconds,
        throughput_ratio=len(complete_in_window) / len(records),
        late_completions=len(successes) - len(complete_in_window),
        per_workload={},
    )
    # Tail latency is over the entire arrival cohort, including drained requests.
    # Failures remain explicit violations and participate in error-rate gates.
    for kind in config["mixtures"][metadata["mixture"]]:
        cohort = [r for r in records if r["workload"] == kind]
        tails = {}
        if not cohort:
            invalid.append(f"no requests sampled for {kind}")
        for metric, threshold in config["slos"][kind].items():
            values = [
                r[metric]
                for r in cohort
                if r["status"] == "success"
                and isinstance(r.get(metric), (int, float))
                and math.isfinite(r[metric])
            ]
            tails[metric] = {
                f"p{p}": percentile(values, p / 100) if values else None for p in (50, 95, 99)
            }
            if not values or tails[metric]["p99"] > threshold:
                unstable.append(f"{kind} p99 {metric} exceeds SLO or is missing")
        summary["per_workload"][kind] = {"count": len(cohort), "latency": tails}
    cap = config["capacity"]
    for key in ("error_rate", "timeout_rate"):
        if summary[key] > cap["max_" + key] + 1e-12:
            unstable.append(key + " exceeds threshold")
    if summary["throughput_ratio"] < cap["min_throughput_ratio"]:
        unstable.append("completed throughput cannot keep up with the offered cohort")
    prometheus = read_json(root / "prometheus.json")
    if (
        prometheus["run_id"] != metadata["run_id"]
        or prometheus["start"] != start
        or prometheus["end"] != end
    ):
        invalid.append("Prometheus run/window mismatch")
    invalid.extend(prometheus.get("errors", []))
    queries = prometheus["queries"]
    try:
        growth = queue_growth(queries["waiting"]["values"], start, end)
        summary["queue_growth_rps"] = growth
        summary["queue_mean"] = sum(v for _, v in queries["waiting"]["values"]) / len(
            queries["waiting"]["values"]
        )
        summary["kv_usage_peak"] = max(v for _, v in queries["kv_usage"]["values"])
        if growth > cap["max_queue_growth_rps"]:
            unstable.append("waiting queue grows in the second half")
        for key in ("prompt_tokens", "generation_tokens"):
            values = queries[key]["values"]
            summary[key + "_server_rate"] = (values[-1][1] - values[0][1]) / (
                values[-1][0] - values[0][0]
            )
    except (KeyError, ValueError, ZeroDivisionError) as error:
        invalid.append("missing queue/KV/counter evidence: " + type(error).__name__)
    summary["valid"] = not invalid
    summary["stable"] = not invalid and not unstable
    write_json(root / "benchmark-summary.json", summary)
    return summary


def capacity_brackets(summaries, expected_repeats):
    groups = defaultdict(lambda: defaultdict(list))
    for result in summaries:
        groups[result["mixture"]][result["offered_rps"]].append(result)
    output = {}
    for mixture, points in sorted(groups.items()):
        stable, first_unstable, rows, interrupted = [], None, [], False
        for rate, runs in sorted(points.items()):
            complete = (
                len(runs) == expected_repeats
                and {r["repeat"] for r in runs} == set(range(expected_repeats))
                and all(r["valid"] for r in runs)
            )
            state = "invalid"
            if complete:
                state = (
                    "stable"
                    if all(r["stable"] for r in runs)
                    else ("unstable" if all(not r["stable"] for r in runs) else "inconsistent")
                )
            if state == "stable" and not interrupted:
                stable.append(rate)
            else:
                interrupted = True
            if state == "unstable" and first_unstable is None:
                first_unstable = rate
            rows.append(
                {"offered_rps": rate, "state": state, "run_ids": [r["run_id"] for r in runs]}
            )
        output[mixture] = {
            "points": rows,
            "max_stable_rps": max(stable) if stable else None,
            "first_unstable_rps": first_unstable,
            "low_rps": min(stable) if stable else None,
        }
    return output


def draw_figures(summaries, output):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for field, filename in [
        ("achieved_rps", "offered-load-vs-achieved-throughput.png"),
        ("goodput_rps", "offered-load-vs-goodput.png"),
        ("queue_mean", "offered-load-vs-queue-depth.png"),
        ("p99_ttft_ms", "offered-load-vs-p99-ttft.png"),
    ]:
        fig, ax = plt.subplots()
        for mixture in sorted({r["mixture"] for r in summaries}):
            runs = [r for r in summaries if r["valid"] and r["mixture"] == mixture]
            values = []
            for r in runs:
                value = r.get(field)
                if field == "p99_ttft_ms":
                    tails = [v["latency"]["ttft_ms"]["p99"] for v in r["per_workload"].values()]
                    value = max((v for v in tails if v is not None), default=None)
                if value is not None:
                    values.append((r["offered_rps"], value))
            if values:
                ax.scatter(*zip(*values), label=mixture)
        ax.set(xlabel="Offered requests/s", ylabel=field)
        if ax.collections:
            ax.legend()
        fig.tight_layout()
        fig.savefig(output / filename)
        plt.close(fig)
    valid = next((r for r in summaries if r["valid"]), None)
    if valid:
        root = Path(valid["path"])
        meta, prom = read_json(root / "metadata.json"), read_json(root / "prometheus.json")
        fig, axes = plt.subplots(3, 1, sharex=True, figsize=(10, 7))
        start = meta["measurement_start"]
        records = read_json(root / "client.json")
        for r in records:
            if r.get("ttft_ms") is not None:
                axes[0].scatter(r["scheduled_at"] - start, r["ttft_ms"], s=8)
        axes[0].set_ylabel("Client TTFT ms")
        for name in ("waiting", "running"):
            values = prom["queries"][name]["values"]
            axes[1].plot([t - start for t, _ in values], [v for _, v in values], label=name)
        axes[1].legend()
        with (root / "gpu.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        axes[2].plot(
            [float(r["timestamp"]) - start for r in rows],
            [float(r["utilization_pct"]) for r in rows],
        )
        axes[2].set(xlabel="Seconds from measurement start", ylabel="GPU utilization %")
        fig.tight_layout()
        fig.savefig(output / "run-timeline-client-server-gpu.png")
        plt.close(fig)


def analyze_directory(root, *, figures=True):
    root = Path(root)
    paths = sorted(root.glob("raw/sessions/*/runs/*/metadata.json"))
    if not paths:
        raise ValueError("no measured runs found")
    metas = [read_json(path) for path in paths]
    configs = {m["config_fingerprint"] for m in metas}
    identities = {
        fingerprint(
            {
                "argv": m["server"]["argv"],
                "runtime": m["server"]["runtime"],
                "source": m["server"]["source"],
            }
        )
        for m in metas
    }
    if len(configs) != 1 or len(identities) != 1:
        raise ValueError("mixed experiment identities; analyze each experiment separately")
    summaries = [analyze_run(p.parent) for p in paths]
    config = metas[0]["config"]
    brackets = capacity_brackets(summaries, config["load"]["repeats"])
    result = {
        "schema_version": 1,
        "config_fingerprint": configs.pop(),
        "capacity": brackets,
        "runs": summaries,
    }
    write_json(root / "analysis.json", result)
    handoff = {
        "schema_version": 1,
        "ready": True,
        "week05_config": config,
        "baseline_server": metas[0]["server"],
        "load_points": {},
        "analysis_fingerprint": fingerprint(result),
    }
    for name in config["mixtures"]:
        bracket = brackets.get(name, {})
        required = ("low_rps", "max_stable_rps", "first_unstable_rps")
        expected = {
            config["calibration"]["baseline_rps"] * m for m in config["load"]["multipliers"]
        }
        observed = {p["offered_rps"] for p in bracket.get("points", [])}
        if (
            not all(bracket.get(k) is not None for k in required)
            or bracket.get("low_rps") == bracket.get("max_stable_rps")
            or expected != observed
            or any(p["state"] not in {"stable", "unstable"} for p in bracket.get("points", []))
            or any(
                p["state"] == "stable" and p["offered_rps"] > bracket["first_unstable_rps"]
                for p in bracket.get("points", [])
                if bracket.get("first_unstable_rps") is not None
            )
        ):
            handoff["ready"] = False
        else:
            handoff["load_points"][name] = dict(
                zip(("low", "boundary", "overload"), (bracket[k] for k in required))
            )
    write_json(root / "handoff.json", handoff)
    if figures:
        draw_figures(summaries, root / "figures")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="results/week05")
    parser.add_argument("--no-figures", action="store_true")
    args = parser.parse_args()
    result = analyze_directory(args.root, figures=not args.no_figures)
    print(result["capacity"])


if __name__ == "__main__":
    main()
