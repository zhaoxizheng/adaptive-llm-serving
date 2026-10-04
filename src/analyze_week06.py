"""Compare controlled tuning cells and gate a deployable operating point."""

from __future__ import annotations

import argparse
import re
import statistics
from pathlib import Path

from src.analyze_week05 import analyze_run
from src.common import read_json, write_json
from src.study_contract import fingerprint


def model_memory_gib(log):
    patterns = [
        r"Model loading took\s+([\d.]+)\s+GiB",
        r"Loading model weights took\s+([\d.]+)\s+GB",
    ]
    for pattern in patterns:
        match = re.search(pattern, log)
        if match:
            return float(match.group(1))
    return None


def confirm_cell(cell, config):
    runs = cell["runs"]
    boundary = [
        r for r in runs if r["tags"]["load_point"] == "boundary" and not r["tags"].get("soak")
    ]
    soak = [r for r in runs if r["tags"].get("soak")]
    expected = cell["repeats"]
    reasons = []
    if (
        len(boundary) != expected
        or {r["repeat"] for r in boundary} != set(range(expected))
        or not all(r["stable"] for r in boundary)
    ):
        reasons.append("boundary needs all stable repeats")
    if len(soak) != 1 or not soak[0]["stable"]:
        reasons.append("stable soak evidence missing")
    for r in soak:
        meta = read_json(Path(r["path"]) / "metadata.json")
        if (
            meta["measurement_end"] - meta["measurement_start"]
            < config["confirmation"]["soak_seconds"]
        ):
            reasons.append("soak shorter than configured duration")
    if any(
        r.get("kv_usage_peak", 1) > 1 - config["confirmation"]["min_headroom_fraction"]
        for r in boundary + soak
    ):
        reasons.append("insufficient measured KV-cache headroom")
    if not cell["quality_passed"]:
        reasons.append("output sanity failed")
    if cell["manual_review"] != "passed":
        reasons.append("manual output review pending")
    if cell["status"] != "completed":
        reasons.append("experiment did not complete")
    return reasons


def analyze(root, *, figures=True):
    root = Path(root)
    cells = []
    comparison_ids = set()
    for path in sorted(root.glob("raw/sessions/*/experiment.json")):
        experiment = read_json(path)
        session = path.parent
        tags, config = experiment["tags"], experiment["config"]
        quality = (
            read_json(session / "quality.json") if (session / "quality.json").is_file() else {}
        )
        runs = [analyze_run(p.parent) for p in sorted(session.glob("runs/*/metadata.json"))]
        cell = {
            "session": str(session),
            **tags,
            "status": experiment["status"],
            "runs": runs,
            "quality_passed": quality.get("passed", False),
            "manual_review": quality.get("manual_review", "pending"),
            "repeats": experiment["handoff"]["week05_config"]["load"]["repeats"],
            "model_memory_gib": (
                model_memory_gib((session / "server.log").read_text())
                if (session / "server.log").is_file()
                else None
            ),
        }
        server = read_json(session / "server.json") if (session / "server.json").is_file() else {}
        cell["server_argv"] = server.get("argv")
        cell["startup_seconds"] = server.get("startup_seconds")
        if runs and quality.get("passed"):
            comparison_ids.add(
                fingerprint(
                    {
                        "handoff": experiment["handoff"],
                        "runtime": server.get("runtime"),
                        "code_files": {
                            p: h
                            for p, h in server.get("source", {}).get("source_files", {}).items()
                            if p.startswith(("src/", "scripts/", "requirements"))
                        },
                        "quality_suite": quality.get("suite_fingerprint"),
                        "variants": config["variants"],
                        "confirmation": config["confirmation"],
                        "tokenizer": config["tokenizer"],
                    }
                )
            )
        boundary = [
            r
            for r in runs
            if r["valid"]
            and r["mixture"] == "mixed"
            and r["tags"]["load_point"] == "boundary"
            and not r["tags"].get("soak")
        ]
        cell["boundary_goodput_rps"] = (
            statistics.mean(r["goodput_rps"] for r in boundary) if boundary else None
        )
        price = config["cost"]["hourly_usd"]
        goodput = cell["boundary_goodput_rps"]
        cell["usd_per_1000_good_requests"] = (
            price * 1000 / (3600 * goodput) if price is not None and goodput else None
        )
        cell["confirmation_reasons"] = (
            confirm_cell(cell, config) if tags["phase"] == "confirm" else ["not a confirmation run"]
        )
        cells.append(cell)
    if not cells:
        raise ValueError("no Week 6 sessions")
    candidates = [c for c in cells if c["phase"] == "confirm" and c["role"] == "candidate"]
    fallbacks = [c for c in cells if c["phase"] == "confirm" and c["role"] == "fallback"]
    result = {
        "schema_version": 1,
        "cells": cells,
        "operating_point": None,
        "fallback": None,
        "selection_reasons": [],
        "comparable": len(comparison_ids) == 1,
    }
    if len(comparison_ids) != 1:
        result["selection_reasons"].append(
            "sessions do not share runtime/handoff/model/quality identities"
        )
    if len(candidates) != 1 or len(fallbacks) != 1:
        result["selection_reasons"].append(
            "need exactly one candidate and one fallback confirmation"
        )
    elif result["comparable"]:
        candidate, fallback = candidates[0], fallbacks[0]
        if not fallback["confirmation_reasons"]:
            result["fallback"] = fallback["server_argv"]
        reasons = candidate["confirmation_reasons"] + fallback["confirmation_reasons"]
        c, b = candidate["boundary_goodput_rps"], fallback["boundary_goodput_rps"]
        c_mem, b_mem = candidate["model_memory_gib"], fallback["model_memory_gib"]
        gain = config["confirmation"]["min_goodput_gain_fraction"]
        saving = config["confirmation"]["min_memory_saving_fraction"]
        repeat_goodputs = [
            r["goodput_rps"]
            for r in candidate["runs"]
            if r["valid"] and r["tags"]["load_point"] == "overload"
        ]
        repeat_baseline = [
            r["goodput_rps"]
            for r in fallback["runs"]
            if r["valid"] and r["tags"]["load_point"] == "overload"
        ]
        repeat_gain = bool(
            repeat_goodputs
            and repeat_baseline
            and min(repeat_goodputs) > 0
            and len(repeat_goodputs) == candidate["repeats"]
            and len(repeat_baseline) == fallback["repeats"]
            and min(repeat_goodputs) >= max(repeat_baseline) * (1 + gain)
        )
        memory_gain = bool(
            c
            and b
            and c >= b * 0.95
            and c_mem is not None
            and b_mem
            and c_mem <= b_mem * (1 - saving)
        )
        if not repeat_gain and not memory_gain:
            reasons.append("no repeatable goodput gain or measured weight-memory saving")
        result["selection_reasons"].extend(sorted(set(reasons)))
        if not reasons:
            result["operating_point"] = candidate["server_argv"]
    write_json(root / "analysis.json", result)
    if figures:
        draw(cells, root / "figures")
    return result


def draw(cells, output):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output.mkdir(parents=True, exist_ok=True)
    for field, phase, filename in [
        ("model_memory_gib", "representation", "variant-vs-model-memory.png"),
        ("boundary_goodput_rps", "representation", "variant-vs-goodput.png"),
        ("boundary_goodput_rps", "sequences", "max-num-seqs-sensitivity.png"),
        ("boundary_goodput_rps", "tokens", "max-num-batched-tokens-sensitivity.png"),
    ]:
        rows = [c for c in cells if c["phase"] == phase and c[field] is not None]
        if not rows:
            continue
        fig, ax = plt.subplots()
        ax.bar([c["variant"] + str(c["overrides"]) for c in rows], [c[field] for c in rows])
        ax.set_ylabel(field)
        ax.tick_params(axis="x", labelrotation=20)
        fig.tight_layout()
        fig.savefig(output / filename)
        plt.close(fig)
    fig, axes = plt.subplots(2, 1, figsize=(10, 7))
    for ax, metric in zip(axes, ("ttft_ms", "tpot_ms")):
        for cell in cells:
            if cell["phase"] != "representation":
                continue
            values = [
                (r["offered_rps"], w["latency"][metric]["p99"])
                for r in cell["runs"]
                if r["valid"]
                for w in r["per_workload"].values()
                if w["latency"][metric]["p99"] is not None
            ]
            if values:
                ax.scatter(*zip(*values), label=cell["variant"])
        ax.set(xlabel="Offered requests/s", ylabel="p99 " + metric)
        if ax.collections:
            ax.legend()
    fig.tight_layout()
    fig.savefig(output / "variant-vs-p99-ttft-tpot.png")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="results/week06")
    parser.add_argument("--no-figures", action="store_true")
    args = parser.parse_args()
    result = analyze(args.root, figures=not args.no_figures)
    print({"operating_point": result["operating_point"], "reasons": result["selection_reasons"]})


if __name__ == "__main__":
    main()
