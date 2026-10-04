"""Validate scheduler decisions and reconstruct request states without simulating policy."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from src.common import read_json, write_json, write_text


def load_events(paths):
    events = []
    for path in paths:
        with Path(path).open() as handle:
            for line in handle:
                if line.strip():
                    event = json.loads(line)
                    if event.get("schema_version") != 1:
                        raise ValueError("unsupported trace schema")
                    if event.get("event") == "trace_truncated":
                        raise ValueError("trace hit event limit and is incomplete")
                    if any(
                        key in event for key in ("prompt", "prompt_ids", "content", "authorization")
                    ):
                        raise ValueError("trace contains prohibited content")
                    if not isinstance(event.get("ts_ns"), int) or event["ts_ns"] < 0:
                        raise ValueError("invalid trace timestamp")
                    if not isinstance(event.get("pid"), int) or event["pid"] <= 0:
                        raise ValueError("invalid process identity")
                    events.append(event)
    return sorted(events, key=lambda e: (e["ts_ns"], e["pid"]))


def parse_events(events, *, require_finished=True):
    states, transitions, steps = {}, [], []
    last_step = -1
    scheduler_pid = None
    allocation_failures, preemptions = [], []
    chunked = set()
    for event in events:
        if event["component"] != "scheduler":
            continue
        if scheduler_pid is None:
            scheduler_pid = event["pid"]
        if event["pid"] != scheduler_pid:
            raise ValueError(
                "multiple scheduler processes; parse each single-instance run separately"
            )
        kind, rid = event["event"], event.get("request_id")

        def transition(request_id, target):
            before = states.get(request_id)
            allowed = {
                None: {"WAITING"},
                "WAITING": {"RUNNING", "FINISHED"},
                "RUNNING": {"RUNNING", "PREEMPTED", "FINISHED"},
                "PREEMPTED": {"RUNNING", "FINISHED"},
                "FINISHED": set(),
            }
            if target not in allowed[before]:
                raise ValueError(f"illegal request transition: {request_id} {before}->{target}")
            states[request_id] = target
            if before != target:
                transitions.append(
                    {
                        "ts_ns": event["ts_ns"],
                        "request_id": request_id,
                        "before": before or "NEW",
                        "after": target,
                        "step": event.get("step", last_step),
                    }
                )

        if kind == "request_queued":
            if event.get("state") != "WAITING":
                raise ValueError("this parser covers ordinary V1 text requests only")
            transition(rid, "WAITING")
        elif kind == "request_freed":
            if not str(event.get("finish_reason", "")).startswith("FINISHED_"):
                raise ValueError("free event must carry a terminal reason")
            if states.get(rid) == "PREEMPTED" and event["finish_reason"] not in {
                "FINISHED_ABORTED",
                "FINISHED_ERROR",
                "FINISHED_IGNORED",
            }:
                raise ValueError("preempted request finished normally without being resumed")
            transition(rid, "FINISHED")
        elif kind == "kv_allocation_failed":
            if states.get(rid) not in {"WAITING", "RUNNING", "PREEMPTED"}:
                raise ValueError("KV failure for unknown or terminal request")
            allocation_failures.append(event)
        elif kind == "step":
            step = event["step"]
            if isinstance(step, bool) or step != last_step + 1:
                raise ValueError("missing, duplicate, or out-of-order scheduler step")
            last_step = step
            scheduled, preempted = event["scheduled"], event["preempted"]
            ids = [s["request_id"] for s in scheduled]
            if len(set(ids)) != len(ids) or len(set(preempted)) != len(preempted):
                raise ValueError("duplicate scheduled/preempted request")
            if set(ids) & set(preempted):
                raise ValueError("request both preempted and scheduled")
            initial, remaining = event["token_budget_initial"], event["token_budget_remaining"]
            counts = [s["tokens"] for s in scheduled]
            if any(isinstance(n, bool) or not isinstance(n, int) or n <= 0 for n in counts):
                raise ValueError("scheduled tokens must be positive integers")
            if not 0 <= sum(counts) <= initial or remaining != initial - sum(counts):
                raise ValueError("token budget invariant failed")
            if len(ids) > event["max_num_seqs"] or event["running_after"] > event["max_num_seqs"]:
                raise ValueError("active sequence limit exceeded")
            running_before = sum(s == "RUNNING" for s in states.values())
            waiting_before = sum(s in {"WAITING", "PREEMPTED"} for s in states.values())
            if (running_before, waiting_before) != (
                event["running_before"],
                event["waiting_before"],
            ):
                raise ValueError("before queues disagree with lifecycle events")
            for request_id in preempted:
                transition(request_id, "PREEMPTED")
                preemptions.append({"step": step, "request_id": request_id})
            for item in scheduled:
                transition(item["request_id"], "RUNNING")
                if item["computed_tokens"] < item["prompt_tokens"] and (
                    item["computed_tokens"] + item["tokens"] < item["prompt_tokens"]
                ):
                    chunked.add(item["request_id"])
            running_after = sum(s == "RUNNING" for s in states.values())
            waiting_after = sum(s in {"WAITING", "PREEMPTED"} for s in states.values())
            if (running_after, waiting_after) != (event["running_after"], event["waiting_after"]):
                raise ValueError("after queues disagree with scheduling decisions")
            steps.append(
                {
                    "step": step,
                    "ts_ns": event["ts_ns"],
                    "scheduled_tokens": sum(counts),
                    "token_budget_initial": initial,
                    "token_budget_remaining": remaining,
                    "running": running_after,
                    "waiting": waiting_after,
                    "preemptions": len(preempted),
                    "kv_usage": event["kv_usage"],
                }
            )
    unfinished = sorted(rid for rid, state in states.items() if state != "FINISHED")
    if not steps or not states:
        raise ValueError("no complete scheduler trace found")
    if unfinished and require_finished:
        raise ValueError(
            f"unfinished or preempted requests without terminal evidence: {unfinished}"
        )
    return {
        "steps": steps,
        "transitions": transitions,
        "final_states": states,
        "unfinished": unfinished,
        "allocation_failures": allocation_failures,
        "preemptions": preemptions,
        "chunked_requests": sorted(chunked),
    }


def write_tables(result, output):
    output.mkdir(parents=True, exist_ok=True)
    for key in ("steps", "transitions"):
        rows = result[key]
        if rows:
            with (output / f"{key}.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
    write_json(output / "analysis.json", result)


def draw(result, output, client=None):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output.mkdir(parents=True, exist_ok=True)
    steps = result["steps"]
    for fields, filename in [
        (["scheduled_tokens", "token_budget_initial"], "step-scheduled-tokens.png"),
        (["running", "waiting"], "step-running-waiting.png"),
        (["token_budget_remaining", "preemptions", "kv_usage"], "token-vs-kv-pressure.png"),
    ]:
        fig, axes = plt.subplots(len(fields), 1, sharex=True, squeeze=False, figsize=(10, 6))
        for ax, field in zip(axes[:, 0], fields):
            ax.step([s["step"] for s in steps], [s[field] for s in steps], where="post")
            ax.set_ylabel(field)
        axes[-1, 0].set_xlabel("Scheduler step")
        fig.tight_layout()
        fig.savefig(output / filename)
        plt.close(fig)
    fig, ax = plt.subplots(figsize=(10, 6))
    ids = sorted(result["final_states"])
    for state in ("WAITING", "RUNNING", "PREEMPTED", "FINISHED"):
        events = [e for e in result["transitions"] if e["after"] == state]
        if events:
            ax.scatter(
                [e["step"] for e in events],
                [ids.index(e["request_id"]) for e in events],
                label=state,
            )
    ax.set_yticks(range(len(ids)), ids)
    ax.set_xlabel("Scheduler step (state transition events)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(output / "request-state-timeline.png")
    plt.close(fig)
    if client:
        fig, axes = plt.subplots(2, 1, sharex=True, figsize=(10, 6))
        start = min(e["ts_ns"] for e in result["transitions"])
        axes[0].step(
            [(s["ts_ns"] - start) / 1e6 for s in steps], [s["waiting"] for s in steps], where="post"
        )
        for row in client:
            if row.get("ttft_ms") is not None:
                axes[1].scatter(
                    (row["scheduled_mono_ns"] - start) / 1e6,
                    row["ttft_ms"],
                    label=row["request_id"],
                )
        axes[0].set_ylabel("Internal waiting")
        axes[1].set(xlabel="Monotonic time from first request (ms)", ylabel="Client TTFT (ms)")
        axes[1].legend()
        fig.tight_layout()
        fig.savefig(output / "internal-queue-vs-client-ttft.png")
        plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--client")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--no-figures", action="store_true")
    args = parser.parse_args()
    output = Path(args.output)
    try:
        result = parse_events(
            load_events(sorted(Path(args.trace_dir).glob("trace-*.jsonl"))),
            require_finished=not args.allow_partial,
        )
    except (ValueError, KeyError) as error:
        write_text(output / "invalid.txt", str(error) + "\n")
        raise
    write_tables(result, output)
    if not args.no_figures:
        draw(result, output / "figures", read_json(args.client) if args.client else None)


if __name__ == "__main__":
    main()
