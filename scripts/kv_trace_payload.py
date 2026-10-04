"""Copied to vllm/_study_kv.py; inactive unless VLLM_KV_TRACE_DIR is set."""

import contextvars
import functools
import hashlib
import json
import os
import threading
import time
from pathlib import Path

_REQUEST = contextvars.ContextVar("kv_request", default=None)
_STEP = -1
_COUNT = 0


def enabled():
    return bool(os.environ.get("VLLM_KV_TRACE_DIR"))


def emit(event, **fields):
    global _COUNT
    if not enabled():
        return
    limit = int(os.environ.get("VLLM_KV_TRACE_LIMIT", "100000"))
    if _COUNT > limit:
        return
    if _COUNT == limit:
        event, fields = "trace_truncated", {}
    record = dict(
        schema_version=1,
        component="kv",
        event=event,
        ts_ns=time.monotonic_ns(),
        pid=os.getpid(),
        thread=threading.get_ident(),
        request_id=_REQUEST.get(),
        step=_STEP,
        **fields,
    )
    root = Path(os.environ["VLLM_KV_TRACE_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"kv-{os.getpid()}.jsonl").open("a") as handle:
        handle.write(json.dumps(record, allow_nan=False) + "\n")
    _COUNT += 1


def scheduler_step():
    global _STEP
    if enabled():
        _STEP += 1


def snapshot(pool, blocks):
    result = []
    for block in blocks:
        if block.is_null:
            continue
        key = block.block_hash
        result.append(
            dict(
                block_id=block.block_id,
                ref_count=block.ref_cnt,
                hash_tag=(
                    None if key is None else hashlib.sha256(repr(key).encode()).hexdigest()[:16]
                ),
                mapped=key is not None
                and pool.cached_block_hash_to_block.get(key, {}).get(block.block_id) is block,
            )
        )
    return result


def pool_event(pool, event, blocks):
    if enabled():
        emit(event, blocks=snapshot(pool, blocks), free_blocks=pool.get_num_free_blocks())


def pool_init(pool):
    if enabled():
        emit(
            "pool_init",
            capacity=pool.num_gpu_blocks,
            null_block=pool.null_block.block_id,
            free_blocks=pool.get_num_free_blocks(),
        )


def evict(pool, block, old_hash):
    if enabled():
        emit(
            "evict",
            blocks=snapshot(pool, [block]),
            old_mapping_present=block.block_id in pool.cached_block_hash_to_block.get(old_hash, {}),
        )


def lookup(pool, blocks):
    if enabled():
        emit("lookup", blocks=snapshot(pool, blocks or []), hit=blocks is not None)


def manager_call(method):
    """Observe manager boundaries without changing arguments or return values."""

    @functools.wraps(method)
    def wrapped(self, request, *args, **kwargs):
        if not enabled():
            return method(self, request, *args, **kwargs)
        token = _REQUEST.set(request.request_id)
        try:
            if self.num_kv_cache_groups != 1:
                raise ValueError("Week 9 trace requires one full-attention KV group")
            size = self.kv_cache_config.kv_cache_groups[0].kv_cache_spec.block_size
            before = self.block_pool.get_num_free_blocks()
            result = method(self, request, *args, **kwargs)
            kind = method.__name__
            if kind == "get_computed_blocks":
                blocks, count = result
                emit(
                    "computed",
                    block_ids=blocks.get_block_ids()[0],
                    cached_tokens=count,
                    prompt_tokens=request.num_prompt_tokens,
                    block_size=size,
                )
            else:
                ids = [] if kind == "free" else self.get_block_ids(request.request_id)[0]
                emit(
                    "request_table",
                    operation=kind,
                    block_ids=ids,
                    block_size=size,
                    requested_tokens=(args[0] if args else kwargs.get("num_new_tokens", 0)),
                    computed_tokens=request.num_computed_tokens,
                    cached_tokens=(
                        args[1] if len(args) > 1 else kwargs.get("num_new_computed_tokens", 0)
                    ),
                    allocation_succeeded=kind != "allocate_slots" or result is not None,
                    free_blocks_before=before,
                    free_blocks_after=self.block_pool.get_num_free_blocks(),
                    finish_reason=request.status.name,
                )
            return result
        finally:
            _REQUEST.reset(token)

    return wrapped
