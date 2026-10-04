"""Reproducibly build trace-only patches against the exact v0.10.2 source."""

from __future__ import annotations

import argparse
import difflib
import subprocess
from pathlib import Path

from src.common import write_text
from src.study_contract import VLLM_COMMIT


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise ValueError(f"source anchor is not unique: {before[:90]!r}")
    return text.replace(before, after, 1)


def patch_text(before, after):
    chunks = []
    for name in sorted(after):
        old = before.get(name, "")
        if old != after[name]:
            chunks.append(f"diff --git a/{name} b/{name}\n")
            if not old:
                chunks.append("new file mode 100644\n")
            chunks.extend(
                difflib.unified_diff(
                    old.splitlines(True),
                    after[name].splitlines(True),
                    fromfile=f"a/{name}" if old else "/dev/null",
                    tofile=f"b/{name}",
                )
            )
    return "".join(chunks)


def build(checkout, output):
    checkout, output = Path(checkout), Path(output)
    commit = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    if commit != VLLM_COMMIT:
        raise ValueError("source checkout does not match the pinned vLLM commit")
    names = [
        "vllm/entrypoints/openai/serving_completion.py",
        "vllm/v1/engine/async_llm.py",
        "vllm/v1/engine/core.py",
        "vllm/v1/core/sched/scheduler.py",
    ]
    original = {
        name: subprocess.check_output(
            ["git", "-C", str(checkout), "show", f"{VLLM_COMMIT}:{name}"], text=True
        )
        for name in names
    }
    week7 = dict(original)
    for name in names:
        # Insert after the license comments, before regular imports.
        lines = week7[name].splitlines(True)
        index = next(i for i, line in enumerate(lines) if line.startswith(("import ", "from ")))
        lines.insert(
            index, "from vllm._study_trace import emit as study_emit, enabled as study_enabled\n"
        )
        week7[name] = "".join(lines)
    week7["vllm/_study_trace.py"] = Path("scripts/trace_payload.py").read_text()
    api, async_llm, core, scheduler = names
    week7[api] = replace_once(
        week7[api],
        "        created_time = int(time.time())\n",
        "        study_emit('api', 'request_received', request_id)\n"
        "        created_time = int(time.time())\n",
    )
    week7[api] = replace_once(
        week7[api],
        "        return response\n",
        "        study_emit('api', 'response_completed', request_id)\n        return response\n",
    )
    anchor = "                    response_json = chunk.model_dump_json(exclude_unset=False)\n"
    week7[api] = replace_once(
        week7[api],
        anchor,
        "                    study_emit('api', 'http_chunk', request_id)\n" + anchor,
    )
    anchor = "        await self.engine_core.add_request_async(request)\n"
    week7[async_llm] = replace_once(
        week7[async_llm],
        anchor,
        "        study_emit('async_llm', 'ipc_submit', request.request_id,\n"
        "                   prompt_tokens=len(request.prompt_token_ids))\n" + anchor,
    )
    # generate() and encode() share this output boundary; both carry metadata only.
    week7[async_llm] = week7[async_llm].replace(
        "                yield out\n",
        "                study_emit('async_llm', 'async_output', request_id, finished=out.finished)\n"
        "                yield out\n",
    )
    anchor = "        all_request_ids = self.output_processor.abort_requests(request_ids)\n"
    week7[async_llm] = replace_once(
        week7[async_llm],
        anchor,
        "        for study_id in request_ids:\n"
        "            study_emit('async_llm', 'abort_sent', study_id)\n" + anchor,
    )
    anchor = "        self.scheduler.add_request(request)\n"
    week7[core] = replace_once(
        week7[core],
        anchor,
        "        study_emit('engine_core', 'core_received', request.request_id)\n" + anchor,
    )
    anchor = "        self.scheduler.finish_requests(request_ids,\n"
    week7[core] = replace_once(
        week7[core],
        anchor,
        "        for study_id in request_ids:\n"
        "            study_emit('engine_core', 'abort_received', study_id)\n" + anchor,
    )
    anchor = "        return (engine_core_outputs,\n"
    week7[core] = replace_once(
        week7[core],
        anchor,
        "        if study_enabled():\n"
        "            for study_batch in engine_core_outputs.values():\n"
        "                for study_output in study_batch.outputs:\n"
        "                    study_emit('engine_core', 'core_output', study_output.request_id,\n"
        "                               num_output_tokens=len(study_output.new_token_ids))\n\n"
        + anchor,
    )
    anchor = "        self.requests[request.request_id] = request\n"
    week7[scheduler] = replace_once(
        week7[scheduler],
        anchor,
        anchor + "        study_emit('scheduler', 'request_queued', request.request_id,\n"
        "                   state=request.status.name)\n",
    )
    anchor = "        del self.requests[request.request_id]\n"
    week7[scheduler] = replace_once(
        week7[scheduler],
        anchor,
        anchor + "        study_emit('scheduler', 'request_freed', request.request_id,\n"
        "                   finish_reason=request.status.name)\n",
    )
    week8 = dict(week7)
    anchor = "        scheduled_new_reqs: list[Request] = []\n"
    week8[scheduler] = replace_once(
        week8[scheduler],
        anchor,
        "        if study_enabled():\n"
        "            self._study_step = getattr(self, '_study_step', -1) + 1\n"
        "            study_running_before = len(self.running)\n"
        "            study_waiting_before = len(self.waiting)\n\n" + anchor,
    )
    anchor = "                if new_blocks is None:\n                    # The request cannot be scheduled.\n"
    if week8[scheduler].count(anchor) != 2:
        raise ValueError("expected two KV allocation failure boundaries")
    week8[scheduler] = week8[scheduler].replace(
        anchor,
        "                if new_blocks is None:\n"
        "                    study_emit('scheduler', 'kv_allocation_failed', request.request_id,\n"
        "                               reason='allocate_slots_returned_none')\n"
        "                    # The request cannot be scheduled.\n",
    )
    anchor = "        self._update_after_schedule(scheduler_output)\n"
    week8[scheduler] = replace_once(
        week8[scheduler],
        anchor,
        "        if study_enabled():\n"
        "            study_emit(\n"
        "                'scheduler', 'step', step=self._study_step,\n"
        "                running_before=study_running_before,\n"
        "                waiting_before=study_waiting_before,\n"
        "                running_after=len(self.running), waiting_after=len(self.waiting),\n"
        "                token_budget_initial=self.max_num_scheduled_tokens,\n"
        "                token_budget_remaining=token_budget,\n"
        "                max_num_seqs=self.max_num_running_reqs,\n"
        "                kv_usage=self.kv_cache_manager.usage,\n"
        "                preempted=[r.request_id for r in preempted_reqs],\n"
        "                scheduled=[dict(request_id=rid, tokens=count,\n"
        "                                computed_tokens=self.requests[rid].num_computed_tokens,\n"
        "                                prompt_tokens=self.requests[rid].num_prompt_tokens)\n"
        "                           for rid, count in num_scheduled_tokens.items()])\n\n" + anchor,
    )
    output.mkdir(parents=True, exist_ok=True)
    write_text(output / "week07-request-trace.patch", patch_text(original, week7))
    write_text(output / "week08-scheduler-trace.patch", patch_text(week7, week8))
    return week7, week8


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", default="patches")
    args = parser.parse_args()
    build(args.source, args.output)


if __name__ == "__main__":
    main()
