import copy
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from scripts import execution_trace_payload as execution_payload
from scripts import kv_trace_payload
from scripts.build_trace_patches import insert_import
from src.deep_study_contract import load_config, make_jobs, override_command
from src.compare_execution_runs import compare
from src.parse_execution_trace import parse_events as parse_execution
from src.parse_kv_trace import parse_events as parse_kv
from src.parse_scheduler_trace import load_events
from src.profile_tables import paired_metrics, union_intervals
from src.summarize_nsys import read_timeline, summarize as summarize_nsys
from src.summarize_torch_profile import summarize as summarize_torch


def event(kind, component="kv", step=0, rid="r1", **fields):
    return dict(
        schema_version=1,
        component=component,
        event=kind,
        step=step,
        pid=10,
        thread=1,
        ts_ns=1,
        request_id=rid,
        **fields,
    )


def block(bid=1, ref=1, tag=None):
    return dict(block_id=bid, ref_count=ref, hash_tag=tag, mapped=tag is not None)


def table(
    rid="r1",
    step=0,
    ids=None,
    op="allocate_slots",
    computed=0,
    cached=0,
    requested=4,
    free=3,
    reason="RUNNING",
):
    return event(
        "request_table",
        rid=rid,
        step=step,
        block_ids=[1] if ids is None else ids,
        operation=op,
        block_size=4,
        computed_tokens=computed,
        cached_tokens=cached,
        requested_tokens=requested,
        free_blocks_after=free,
        allocation_succeeded=True,
        finish_reason=reason,
    )


def kv_events():
    return [
        event("pool_init", step=-1, capacity=5, null_block=0, free_blocks=4),
        event("computed", block_ids=[], cached_tokens=0, prompt_tokens=4, block_size=4),
        event("allocate", blocks=[block()], free_blocks=3),
        event("cache", blocks=[block(tag="hash1")], free_blocks=3),
        table(),
        event("release", blocks=[block(ref=0, tag="hash1")], free_blocks=4),
        table(ids=[], op="free", free=4, reason="FINISHED_LENGTH_CAPPED"),
        event("lookup", step=1, rid="r2", blocks=[block(ref=0, tag="hash1")], hit=True),
        event(
            "computed",
            step=1,
            rid="r2",
            block_ids=[1],
            cached_tokens=4,
            prompt_tokens=6,
            block_size=4,
        ),
        event("touch", step=1, rid="r2", blocks=[block(tag="hash1")], free_blocks=3),
        event("allocate", step=1, rid="r2", blocks=[block(bid=2)], free_blocks=2),
        table("r2", 1, ids=[1, 2], cached=4, requested=2, free=2),
        event(
            "release",
            step=1,
            rid="r2",
            blocks=[block(bid=2, ref=0), block(ref=0, tag="hash1")],
            free_blocks=4,
        ),
        table("r2", 1, ids=[], op="free", free=4, reason="FINISHED_ABORTED"),
    ]


def test_free_retains_cache_and_partial_prefix_reuses_only_full_block():
    result = parse_kv(kv_events())
    assert result["final_blocks"][1] == dict(ref_count=0, hash_tag="hash1", mapped=True)
    assert result["prefix_lookups"][1]["cached_tokens"] == 4
    assert result["unfinished"] == []


@pytest.mark.parametrize(
    "corruption", ["negative", "leak", "stale", "partial", "capacity", "owner", "writer"]
)
def test_corrupt_kv_evidence_rejected(corruption):
    events = kv_events()
    if corruption == "negative":
        events[5]["blocks"][0]["ref_count"] = -1
    elif corruption == "leak":
        events = events[:-2]
    elif corruption == "stale":
        events[7]["blocks"][0]["hash_tag"] = "old"
    elif corruption == "partial":
        events[8]["cached_tokens"] = 5
    elif corruption == "capacity":
        events[2]["free_blocks"] = 4
    elif corruption == "owner":
        events[11]["block_ids"] = [1]
    else:
        for row in events[7:]:
            row["step"] = 0
        events[11].update(cached_tokens=0, requested_tokens=6)
    with pytest.raises(ValueError):
        parse_kv(events)


def test_eviction_invalidates_mapping_before_reallocation():
    events = kv_events()[:7] + [
        event("evict", step=1, rid="r2", blocks=[block(ref=0)], old_mapping_present=False),
        event("allocate", step=1, rid="r2", blocks=[block()], free_blocks=3),
        table("r2", 1),
        event("release", step=1, rid="r2", blocks=[block(ref=0)], free_blocks=4),
        table("r2", 1, ids=[], op="free", free=4, reason="FINISHED_LENGTH_CAPPED"),
    ]
    assert parse_kv(events)["evictions"] == 1
    events[7]["old_mapping_present"] = True
    with pytest.raises(ValueError, match="stale"):
        parse_kv(events)


def execution_events(mode="NONE"):
    return [
        event(
            "schedule",
            "execution",
            scheduled=[dict(request_id="r1", tokens=5, computed_tokens=0, prompt_tokens=5)],
        ),
        event(
            "shape",
            "execution",
            request_count=1,
            scheduled_tokens=5,
            prefill_tokens=5,
            decode_tokens=0,
            input_ids_shape=[8],
            positions_shape=[8],
            padded_tokens=8,
            kv_slot_count=5,
            dispatch_mode=mode,
        ),
        event("output", "execution", scheduled_tokens=5),
    ]


def test_graph_requires_replay_evidence_and_accepts_padding():
    events = execution_events("PIECEWISE")
    assert parse_execution(events)["steps"][0]["execution_mode"] == "unproven_graph"
    events.insert(2, event("graph_replay", "execution"))
    result = parse_execution(events)["steps"][0]
    assert result["execution_mode"] == "graph_replay"
    assert result["gpu_execute_us"] is None


@pytest.mark.parametrize(
    "corruption", ["shape", "composition", "missing_output", "missing_schedule", "graph"]
)
def test_execution_evidence_mismatch_is_rejected(corruption):
    events = execution_events()
    if corruption == "shape":
        events[1]["input_ids_shape"] = [4]
    elif corruption == "composition":
        events[1]["prefill_tokens"] = 4
    elif corruption == "missing_output":
        events.pop()
    elif corruption == "missing_schedule":
        events.pop(0)
    else:
        events.append(event("graph_replay", "execution"))
    with pytest.raises(ValueError):
        parse_execution(events)


def test_trace_truncation_is_never_accepted(tmp_path):
    trace = tmp_path / "kv.jsonl"
    trace.write_text(json.dumps(event("trace_truncated")) + "\n")
    with pytest.raises(ValueError, match="incomplete"):
        load_events([trace])


@pytest.mark.parametrize(
    "path",
    [
        "configs/week09.yaml",
        "configs/week10.yaml",
        "configs/week11-profiler.yaml",
        "configs/week12-nsys.yaml",
    ],
)
def test_checked_in_scenarios_validate_without_gpu(path):
    config, _, _, scenarios = load_config(path)
    assert config["week"] in (9, 10, 11, 12)
    assert len(scenarios) >= 4


def test_prefix_jobs_and_cross_week_workloads_are_identical():
    tokenizer = SimpleNamespace(
        get_vocab=lambda: {str(i): i for i in range(100)}, all_special_ids=[0]
    )
    _, _, _, scenarios = load_config("configs/week09.yaml")
    jobs = make_jobs(scenarios["partial"], tokenizer, 42)
    a, b = (j["prompt_ids"] for j in jobs)
    assert len(a) == len(b) == 544 and a[:519] == b[:519] and a[519] != b[519]
    _, _, _, torch_scenarios = load_config("configs/week11-profiler.yaml")
    _, _, _, nsys_scenarios = load_config("configs/week12-nsys.yaml")
    assert make_jobs(torch_scenarios["mixed"], tokenizer, 42) == make_jobs(
        nsys_scenarios["mixed"], tokenizer, 42
    )


def test_boolean_false_does_not_create_unsupported_no_enforce_eager_flag():
    assert override_command(
        ["vllm", "serve", "model", "--enforce-eager"], {"enforce_eager": False}
    ) == ["vllm", "serve", "model"]
    with pytest.raises(ValueError):
        override_command(["vllm"], {"tensor_parallel_size": 2})


def test_payloads_do_not_import_torch_or_emit_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("VLLM_KV_TRACE_DIR", raising=False)
    monkeypatch.delenv("VLLM_EXECUTION_CONFIG", raising=False)
    # An invalid pool would fail immediately if the disabled hook read it.
    kv_trace_payload.pool_init(None)
    kv_trace_payload.pool_event(None, "allocate", [None])
    with execution_payload.phase("prepare"):
        pass
    assert not list(tmp_path.iterdir())


def test_kv_snapshot_omits_tokens_and_checks_actual_mapping():
    block_obj = SimpleNamespace(block_id=1, ref_cnt=0, block_hash=(b"digest", 0), is_null=False)
    pool = SimpleNamespace(cached_block_hash_to_block={block_obj.block_hash: {1: block_obj}})
    snapshot = kv_trace_payload.snapshot(pool, [block_obj])[0]
    assert snapshot["mapped"] and len(snapshot["hash_tag"]) == 16
    assert "digest" not in json.dumps(snapshot)
    pool.cached_block_hash_to_block.clear()
    assert not kv_trace_payload.snapshot(pool, [block_obj])[0]["mapped"]


def test_union_and_single_token_baseline_metrics():
    assert union_intervals([(0, 10), (5, 15), (15, 18)]) == [[0, 18]]
    row = dict(
        request_id="r",
        prompt_sha256="hash",
        status="success",
        output_tokens=1,
        completed_at=2,
        scheduled_at=1,
        ttft_ms=100,
        tpot_ms=None,
    )
    result = paired_metrics([row], [{**row, "completed_at": 2.5}])
    assert result["wall_overhead_ratio"] == 0.5
    assert result["baseline"]["mean_tpot_ms"] is None
    with pytest.raises(ValueError, match="workload differs"):
        paired_metrics([row], [{**row, "prompt_sha256": "different"}])


def test_torch_self_cpu_and_kernel_busy_are_distinct():
    trace = dict(
        traceEvents=[
            dict(cat="kernel", ph="X", name="k", ts=0, dur=10),
            dict(cat="kernel", ph="X", name="k2", ts=5, dur=10),
            dict(cat="cuda_runtime", ph="X", name="cudaDeviceSynchronize", ts=4, dur=9),
        ]
    )
    ops = [dict(name="op", self_cpu_us=2, cpu_total_us=20, self_device_us=20, device_total_us=25)]
    result = summarize_torch(trace, ops)
    assert result["kernel_busy_us"] == 15
    assert result["cpu_operators"][0]["self_cpu_us"] == 2
    assert len(result["synchronization"]) == 1
    with pytest.raises(ValueError, match="lacks CUDA"):
        summarize_torch(dict(traceEvents=[]), ops)


def test_nsys_sqlite_units_union_and_unknown_attribution(tmp_path):
    path = tmp_path / "report.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
        CREATE TABLE StringIds (id INTEGER, value TEXT);
        INSERT INTO StringIds VALUES (1, 'kernel'), (2, 'cudaLaunchKernel');
        CREATE TABLE CUPTI_ACTIVITY_KIND_KERNEL (start INTEGER, end INTEGER, demangledName INTEGER, deviceId INTEGER);
        INSERT INTO CUPTI_ACTIVITY_KIND_KERNEL VALUES (20, 60, 1, 0), (40, 80, 1, 0);
        CREATE TABLE CUPTI_ACTIVITY_KIND_RUNTIME (start INTEGER, end INTEGER, nameId INTEGER);
        INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES (0, 25, 2);
        CREATE TABLE NVTX_EVENTS (start INTEGER, end INTEGER, text TEXT);
        INSERT INTO NVTX_EVENTS VALUES (0, 100, 'study/execute/step=2');
        """
        )
    timeline = read_timeline(path)
    result = summarize_nsys(timeline)
    assert result["gpu_busy_ns"] == 60 and result["gpu_idle_ns"] == 40
    assert result["gaps"][0]["classification"] == "unknown"
    assert "cudaLaunchKernel" in result["gaps"][0]["overlapping_evidence"]["api"]
    invalid = copy.deepcopy(timeline)
    invalid["kernel"][1]["device"] = 1
    with pytest.raises(ValueError, match="one GPU"):
        summarize_nsys(invalid)


def test_nsys_capture_arms_after_warmup_and_stops_once(monkeypatch, tmp_path):
    calls = []
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(
            synchronize=lambda: calls.append("sync"),
            profiler=SimpleNamespace(
                start=lambda: calls.append("start"), stop=lambda: calls.append("stop")
            ),
        )
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.delenv("VLLM_EXECUTION_CONFIG", raising=False)
    cfg = dict(
        mode="nsys",
        wait=1,
        warmup=1,
        active=2,
        output=str(tmp_path),
        arm_file=str(tmp_path / "arm"),
    )
    capture = execution_payload.Capture(cfg)
    assert not capture.before() and not calls
    (tmp_path / "arm").touch()
    for step in range(4):
        token = execution_payload._STEP.set(step)
        try:
            assert capture.before()
            capture.after()
        finally:
            execution_payload._STEP.reset(token)
    assert calls == ["sync", "start", "sync", "stop"]
    assert capture.done and not capture.before()
    saved = json.loads(next(tmp_path.glob("capture-*.json")).read_text())
    assert saved["active_steps"] == [2, 3] and saved["complete"]


def test_graph_comparison_rejects_shape_drift_and_unproven_replay(tmp_path):
    roots = [tmp_path / "eager", tmp_path / "graph"]
    for root, mode in zip(roots, ["eager", "graph_replay"]):
        (root / "capture").mkdir(parents=True)
        (root / "run.json").write_text(
            json.dumps(
                dict(
                    source={"commit": "pinned"},
                    workload_id="same",
                    tokenizer="same",
                    status="collected",
                )
            )
        )
        argv = ["vllm", "serve", "model"] + (["--enforce-eager"] if mode == "eager" else [])
        (root / "capture" / "server.json").write_text(json.dumps(dict(runtime="L4", argv=argv)))
        row = dict(
            composition="decode",
            request_count=1,
            scheduled_tokens=1,
            prefill_tokens=0,
            decode_tokens=1,
            input_ids_shape=[1],
            execution_mode=mode,
        )
        (root / "capture" / "analysis.json").write_text(json.dumps(dict(steps=[row])))
    assert compare(*roots)["logical_shapes_match"]
    target = roots[1] / "capture" / "analysis.json"
    content = json.loads(target.read_text())
    content["steps"][0]["execution_mode"] = "unproven_graph"
    target.write_text(json.dumps(content))
    with pytest.raises(ValueError, match="proven replay"):
        compare(*roots)
    content["steps"][0]["scheduled_tokens"] = 2
    target.write_text(json.dumps(content))
    with pytest.raises(ValueError, match="logical step shapes differ"):
        compare(*roots)


@pytest.mark.parametrize(
    "source",
    [
        "# license\nfrom __future__ import annotations\nimport os\n",
        '"""Module docstring."""\nfrom __future__ import annotations\nimport os\n',
        "# license\nimport os\n",
    ],
)
def test_patch_imports_preserve_future_semantics(source):
    patched = insert_import(source, "from example import hook")
    compile(patched, "patched.py", "exec")
    if "__future__" in source:
        assert patched.index("__future__") < patched.index("example")


def test_manager_records_positional_cached_tokens_used_by_real_scheduler(monkeypatch):
    observed = []
    monkeypatch.setenv("VLLM_KV_TRACE_DIR", "unused")
    monkeypatch.setattr(kv_trace_payload, "emit", lambda kind, **fields: observed.append(fields))

    class Manager:
        num_kv_cache_groups = 1
        kv_cache_config = SimpleNamespace(
            kv_cache_groups=[SimpleNamespace(kv_cache_spec=SimpleNamespace(block_size=4))]
        )
        block_pool = SimpleNamespace(get_num_free_blocks=lambda: 5)

        def get_block_ids(self, request_id):
            return ([1, 2, 3],)

        @kv_trace_payload.manager_call
        def allocate_slots(self, request, num_new_tokens, num_new_computed_tokens=0):
            return object()

    request = SimpleNamespace(
        request_id="r", num_computed_tokens=0, status=SimpleNamespace(name="RUNNING")
    )
    Manager().allocate_slots(request, 4, 8)
    assert observed[-1]["cached_tokens"] == 8
    assert observed[-1]["requested_tokens"] == 4


@pytest.mark.parametrize(
    "field", ["self_device_time_total", "device_time_total", "self_device_memory_usage"]
)
def test_profiler_device_metrics_support_legacy_names_without_silent_zero(field):
    item = SimpleNamespace(**{field.replace("device", "cuda"): 17})
    assert execution_payload.device_metric(item, field) == 17
    with pytest.raises(ValueError, match="lacks"):
        execution_payload.device_metric(SimpleNamespace(), field)
