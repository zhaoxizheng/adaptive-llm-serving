"""Check logical shape control before interpreting eager/graph profile differences."""

import argparse
from collections import Counter
from pathlib import Path

from src.common import read_json, write_json


def compare(left, right):
    left, right = Path(left), Path(right)
    metadata = [read_json(root / "run.json") for root in (left, right)]
    for key in ("source", "workload_id", "tokenizer"):
        if metadata[0][key] != metadata[1][key]:
            raise ValueError(f"A/B {key} differs")
    if any(m["status"] != "collected" for m in metadata):
        raise ValueError("both sessions must be collected")
    servers = [read_json(root / "capture" / "server.json") for root in (left, right)]
    if servers[0]["runtime"] != servers[1]["runtime"]:
        raise ValueError("A/B GPU/software runtime differs")
    normalized = [[a for a in s["argv"] if a != "--enforce-eager"] for s in servers]
    if normalized[0] != normalized[1]:
        raise ValueError("A/B changes flags beyond enforce_eager")
    signatures, padded, modes = [], [], []
    for root in (left, right):
        rows = read_json(root / "capture" / "analysis.json")["steps"]
        captures = list((root / "capture").glob("capture-*.json"))
        if captures:
            if len(captures) != 1:
                raise ValueError("multiple worker captures")
            active = read_json(captures[0])["active_steps"]
            rows = [r for r in rows if r["step"] in active]
        else:
            rows = [r for r in rows if r["composition"] == "decode"]
        if not rows:
            raise ValueError("no comparable decode steps")
        signatures.append(
            Counter(
                (r["request_count"], r["scheduled_tokens"], r["prefill_tokens"], r["decode_tokens"])
                for r in rows
            )
        )
        padded.append(dict(Counter(str(r["input_ids_shape"]) for r in rows)))
        modes.append(set(r["execution_mode"] for r in rows))
    if signatures[0] != signatures[1]:
        raise ValueError("A/B logical step shapes differ")
    if modes[0] != {"eager"} or modes[1] != {"graph_replay"}:
        raise ValueError("A/B needs eager and proven replay on every selected step")
    return dict(
        logical_shapes_match=True,
        eager_padding=padded[0],
        graph_padding=padded[1],
        note="shape-controlled short captures; profiler overhead still needs review",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eager", required=True)
    parser.add_argument("--graph", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    write_json(args.output, compare(args.eager, args.graph))


if __name__ == "__main__":
    main()
