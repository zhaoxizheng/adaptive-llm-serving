"""Build Week 9/10 patches after the unchanged Week 7/8 patch chain."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from scripts.build_trace_patches import build as build_early
from scripts.build_trace_patches import insert_import, patch_text, replace_once
from src.common import write_text
from src.study_contract import VLLM_COMMIT


def read_source(checkout, name):
    return subprocess.check_output(
        ["git", "-C", str(checkout), "show", f"{VLLM_COMMIT}:{name}"], text=True
    )


def decorate(source, method, decorator):
    anchor = f"    def {method}("
    return replace_once(source, anchor, f"    @{decorator}\n" + anchor)


def build(checkout, output):
    _, previous = build_early(checkout, output)
    pool = "vllm/v1/core/block_pool.py"
    manager = "vllm/v1/core/kv_cache_manager.py"
    scheduler = "vllm/v1/core/sched/scheduler.py"
    for name in (pool, manager):
        previous[name] = read_source(checkout, name)
    week9 = dict(previous)
    week9["vllm/_study_kv.py"] = Path("scripts/kv_trace_payload.py").read_text()
    for name in (pool, manager, scheduler):
        week9[name] = insert_import(week9[name], "from vllm import _study_kv as study_kv")
    text = week9[pool]
    text = replace_once(
        text,
        "        self.kv_event_queue: list[KVCacheEvent] = []\n",
        "        self.kv_event_queue: list[KVCacheEvent] = []\n"
        "        study_kv.pool_init(self)\n",
    )
    text = replace_once(
        text,
        "                return None\n",
        "                " "study_kv.lookup(self, None)\n                return None\n",
    )
    text = replace_once(
        text,
        "        return cached_blocks\n",
        "        study_kv.lookup(self, cached_blocks)\n        return cached_blocks\n",
    )
    text = replace_once(
        text,
        "        if self.enable_kv_cache_events:\n            if num_cached_blocks",
        "        study_kv.pool_event(self, 'cache', new_full_blocks)\n\n"
        "        if self.enable_kv_cache_events:\n            if num_cached_blocks",
    )
    text = replace_once(
        text,
        "        return ret\n",
        "        study_kv.pool_event(self, 'allocate', ret)\n        return ret\n",
    )
    text = replace_once(
        text,
        "        return True\n\n    def touch",
        "        study_kv.evict(self, block, block_hash)\n" "        return True\n\n    def touch",
    )
    text = replace_once(
        text,
        "    def free_blocks(",
        "        if study_kv.enabled():\n"
        "            study_kv.pool_event(self, 'touch',\n"
        "                                [b for group in blocks for b in group])\n\n"
        "    def free_blocks(",
    )
    text = replace_once(
        text,
        "    def reset_prefix_cache(",
        "        study_kv.pool_event(self, 'release', blocks_list)\n\n"
        "    def reset_prefix_cache(",
    )
    week9[pool] = text
    for method in ("get_computed_blocks", "allocate_slots", "free", "cache_blocks"):
        week9[manager] = decorate(week9[manager], method, "study_kv.manager_call")
    week9[scheduler] = replace_once(
        week9[scheduler],
        "        scheduled_new_reqs: list[Request] = []\n",
        "        study_kv.scheduler_step()\n        scheduled_new_reqs: list[Request] = []\n",
    )
    write_text(Path(output) / "week09-kv-trace.patch", patch_text(previous, week9))

    runner = "vllm/v1/worker/gpu_model_runner.py"
    graph = "vllm/compilation/cuda_graph.py"
    output_type = "vllm/v1/core/sched/output.py"
    for name in (runner, graph, output_type):
        week9[name] = read_source(checkout, name)
    week10 = dict(week9)
    week10["vllm/_study_execution.py"] = Path("scripts/execution_trace_payload.py").read_text()
    for name in (runner, graph, scheduler):
        week10[name] = insert_import(
            week10[name], "from vllm import _study_execution as study_exec"
        )
    week10[
        output_type
    ] += "\n    # Local study correlation only; absent instrumentation uses -1.\n    study_step: int = -1\n"
    week10[scheduler] = replace_once(
        week10[scheduler],
        "        self._update_after_schedule(scheduler_output)\n",
        "        study_exec.schedule(self, scheduler_output)\n"
        "        self._update_after_schedule(scheduler_output)\n",
    )
    week10[scheduler] = decorate(week10[scheduler], "schedule", "study_exec.scheduler")
    text = decorate(week10[runner], "execute_model", "study_exec.execution")
    for method, phase in (
        ("_update_states", "update_batch"),
        ("_prepare_inputs", "prepare"),
        ("_preprocess", "preprocess"),
        ("_sample", "sample"),
        ("_bookkeeping_sync", "output_copy"),
        ("load_model", "load_model"),
        ("initialize_kv_cache", "initialize_kv"),
        ("capture_model", "capture_model"),
    ):
        text = decorate(text, method, f"study_exec.boundary('{phase}')")
    text = replace_once(
        text,
        "        # Run the model.\n",
        "        study_exec.shape(self, scheduler_output, input_ids, positions,\n"
        "                         num_input_tokens, cudagraph_runtime_mode)\n\n"
        "        # Run the model.\n",
    )
    text = replace_once(
        text,
        '), record_function_or_nullcontext("Forward"),',
        '), study_exec.phase("forward"), record_function_or_nullcontext("Forward"),',
    )
    text = replace_once(
        text,
        "                logits = self.model.compute_logits(sample_hidden_states, None)\n",
        "                with study_exec.phase('logits'):\n"
        "                    logits = self.model.compute_logits(sample_hidden_states, None)\n",
    )
    week10[runner] = text
    week10[graph] = replace_once(
        week10[graph],
        "            entry.cudagraph = cudagraph\n",
        "            entry.cudagraph = cudagraph\n"
        "            study_exec.graph('graph_capture')\n",
    )
    week10[graph] = replace_once(
        week10[graph],
        "        entry.cudagraph.replay()\n",
        "        with study_exec.phase('graph_replay'):\n"
        "            entry.cudagraph.replay()\n"
        "        study_exec.graph('graph_replay')\n",
    )
    for stage in (week9, week10):
        for name, content in stage.items():
            compile(content, name, "exec")
    write_text(Path(output) / "week10-execution-trace.patch", patch_text(week9, week10))
    return week9, week10


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", default="patches")
    args = parser.parse_args()
    build(args.source, args.output)


if __name__ == "__main__":
    main()
