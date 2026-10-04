"""Reconstruct a single full-attention KV pool from observed lifecycle events."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from src.common import write_json
from src.parse_scheduler_trace import load_events


def parse_events(events, *, require_finished=True):
    events = [e for e in events if e["component"] == "kv"]
    if not events or events[0]["event"] != "pool_init":
        raise ValueError("KV trace must begin with pool_init")
    if len({e["pid"] for e in events}) != 1:
        raise ValueError("parse one KV pool process at a time")
    init = events[0]
    capacity, null = init["capacity"], init["null_block"]
    if capacity <= 1 or not 0 <= null < capacity or init["free_blocks"] != capacity - 1:
        raise ValueError("invalid initial free pool")
    blocks = {
        i: dict(ref_count=0, hash_tag=None, mapped=False) for i in range(capacity) if i != null
    }
    tables, terminal, timeline, hits, writers = {}, {}, [], [], {}
    last_step = -1
    evictions = 0
    for event in events[1:]:
        kind, rid, step = event["event"], event.get("request_id"), event["step"]
        if step < last_step:
            raise ValueError("KV step moved backwards")
        if step != last_step:
            writers = {}
        last_step = step
        rows = event.get("blocks", [])
        if len({b["block_id"] for b in rows}) != len(rows):
            raise ValueError("duplicate physical block in event")
        for row in rows:
            bid = row["block_id"]
            if bid not in blocks:
                raise ValueError("invalid physical block ID")
            old = blocks[bid]
            ref, tag = row["ref_count"], row["hash_tag"]
            if isinstance(ref, bool) or not isinstance(ref, int) or ref < 0:
                raise ValueError("negative or invalid reference count")
            if row["mapped"] != (tag is not None):
                raise ValueError("stale hash mapping")
            expected = old["ref_count"] + {"allocate": 1, "touch": 1, "release": -1}.get(kind, 0)
            if ref != expected:
                raise ValueError("reference transition mismatch")
            if kind == "allocate" and (old["ref_count"] or tag is not None):
                raise ValueError("allocated an occupied or unevicted block")
            if kind in {"lookup", "touch"} and (not tag or tag != old["hash_tag"]):
                raise ValueError("cache hit uses a stale or partial block")
            if kind == "cache" and (old["hash_tag"] is not None or tag is None or ref == 0):
                raise ValueError("invalid full-block cache transition")
            if kind == "evict":
                if old["ref_count"] or old["hash_tag"] is None or tag is not None:
                    raise ValueError("invalid eviction")
                if event["old_mapping_present"]:
                    raise ValueError("eviction retained stale mapping")
                evictions += 1
            elif kind in {"release", "lookup", "touch"} and tag != old["hash_tag"]:
                raise ValueError("release/touch unexpectedly changed cached content")
            blocks[bid] = dict(ref_count=ref, hash_tag=tag, mapped=row["mapped"])
        if kind in {"allocate", "touch", "release", "cache"}:
            if event["free_blocks"] != sum(b["ref_count"] == 0 for b in blocks.values()):
                raise ValueError("free queue does not match reference counts")
        elif kind == "computed":
            size, cached, ids = event["block_size"], event["cached_tokens"], event["block_ids"]
            if size <= 0 or cached != len(ids) * size or cached > event["prompt_tokens"] - 1:
                raise ValueError("prefix hit must cover full blocks below prompt length")
            if any(i not in blocks or not blocks[i]["mapped"] for i in ids):
                raise ValueError("computed prefix points at uncached block")
            hits.append(event)
        elif kind == "request_table":
            ids = event["block_ids"]
            if len(set(ids)) != len(ids) or any(i not in blocks for i in ids):
                raise ValueError("invalid request block table")
            if terminal.get(rid):
                raise ValueError("terminal request used KV again")
            tables[rid] = ids
            counts = Counter(b for owned in tables.values() for b in owned)
            if any(counts[i] != b["ref_count"] for i, b in blocks.items()):
                raise ValueError("request ownership differs from pool references")
            if event["free_blocks_after"] != sum(b["ref_count"] == 0 for b in blocks.values()):
                raise ValueError("manager free count mismatch")
            if event["operation"] == "allocate_slots" and event["allocation_succeeded"]:
                size = event["block_size"]
                start = event["computed_tokens"] + event["cached_tokens"]
                stop = start + event["requested_tokens"]
                for logical in range(start // size, (stop + size - 1) // size):
                    if logical >= len(ids):
                        raise ValueError("write exceeds allocated table")
                    bid = ids[logical]
                    if bid in writers and writers[bid] != rid:
                        raise ValueError("two request write paths share a physical block")
                    writers[bid] = rid
            if event["operation"] == "free":
                if ids:
                    raise ValueError("free retained request references")
                terminal[rid] = event["finish_reason"].startswith("FINISHED_")
            timeline.append(event)
        elif kind not in {"allocate", "touch", "release", "cache", "evict", "lookup"}:
            raise ValueError(f"unknown KV event: {kind}")
    unfinished = [rid for rid in tables if not terminal.get(rid)]
    if require_finished and (unfinished or any(b["ref_count"] for b in blocks.values())):
        raise ValueError("unfinished requests or leaked block references")
    if not timeline:
        raise ValueError("no request lifecycle evidence")
    return dict(
        request_timeline=timeline,
        prefix_lookups=hits,
        evictions=evictions,
        unfinished=unfinished,
        final_blocks=blocks,
        scope="one full-attention KV group, ordinary text, no speculative or async scheduling",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()
    result = parse_events(
        load_events(Path(args.trace_dir).glob("kv-*.jsonl")),
        require_finished=not args.allow_partial,
    )
    write_json(args.output, result)


if __name__ == "__main__":
    main()
