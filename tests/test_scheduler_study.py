import copy
import json

import pytest

from scripts import trace_payload
from src.parse_request_trace import validate_path
from src.parse_scheduler_trace import load_events, parse_events


def event(kind, rid="r", **fields):
    return {
        "schema_version": 1,
        "ts_ns": 1,
        "wall_time_utc": "2026-10-04T00:00:00+00:00",
        "pid": 22,
        "thread": 1,
        "component": "scheduler",
        "event": kind,
        "request_id": rid,
        **fields,
    }


def step(number, tokens=4, *, preempted=None, scheduled=True, before=(0, 1), after=(1, 0)):
    return event(
        "step",
        None,
        step=number,
        running_before=before[0],
        waiting_before=before[1],
        running_after=after[0],
        waiting_after=after[1],
        token_budget_initial=4,
        token_budget_remaining=4 - tokens if scheduled else 4,
        max_num_seqs=1,
        kv_usage=0.3,
        preempted=preempted or [],
        scheduled=(
            [{"request_id": "r", "tokens": tokens, "computed_tokens": 0, "prompt_tokens": 8}]
            if scheduled
            else []
        ),
    )


def valid_events():
    return [
        event("request_queued", state="WAITING"),
        step(0),
        event("request_freed", finish_reason="FINISHED_LENGTH_CAPPED"),
    ]


def test_valid_chunked_prefill_and_terminal_lifecycle():
    result = parse_events(valid_events())
    assert result["chunked_requests"] == ["r"]
    assert result["final_states"] == {"r": "FINISHED"}


@pytest.mark.parametrize(
    "change", ["budget", "sequence", "duplicate", "gap", "queue", "reschedule"]
)
def test_corrupt_scheduler_evidence_is_rejected(change):
    events = copy.deepcopy(valid_events())
    if change == "budget":
        events[1]["scheduled"][0]["tokens"] = 5
    elif change == "sequence":
        events[1]["max_num_seqs"] = 0
    elif change == "duplicate":
        events[1]["scheduled"] *= 2
    elif change == "gap":
        events[1]["step"] = 1
    elif change == "queue":
        events[1]["waiting_before"] = 2
    else:
        events.append(step(1, before=(0, 0)))
    with pytest.raises(ValueError):
        parse_events(events)


def test_preempted_request_must_resume_or_abort():
    events = valid_events()[:2] + [
        event("kv_allocation_failed", reason="allocate_slots_returned_none"),
        step(1, scheduled=False, preempted=["r"], before=(1, 0), after=(0, 1)),
    ]
    with pytest.raises(ValueError, match="unfinished"):
        parse_events(events)
    with pytest.raises(ValueError, match="without being resumed"):
        parse_events(events + [event("request_freed", finish_reason="FINISHED_LENGTH_CAPPED")])
    resumed = events + [step(2), event("request_freed", finish_reason="FINISHED_LENGTH_CAPPED")]
    assert len(parse_events(resumed)["preemptions"]) == 1
    assert (
        parse_events(events + [event("request_freed", finish_reason="FINISHED_ABORTED")])[
            "unfinished"
        ]
        == []
    )


def test_trace_is_off_by_default_and_truncation_is_not_accepted(tmp_path, monkeypatch):
    monkeypatch.delenv("VLLM_STUDY_TRACE_DIR", raising=False)
    trace_payload.emit("api", "test", "r")
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setenv("VLLM_STUDY_TRACE_DIR", str(tmp_path))
    monkeypatch.setenv("VLLM_STUDY_TRACE_LIMIT", "1")
    monkeypatch.setattr(trace_payload, "_COUNT", 0)
    trace_payload.emit("api", "request_received", "r")
    with pytest.raises(ValueError, match="allowlisted"):
        trace_payload.emit("api", "bad", "r", prompt="private")
    trace_payload.emit("api", "request_received", "r2")
    with pytest.raises(ValueError, match="incomplete"):
        load_events(tmp_path.glob("*.jsonl"))
    assert "private" not in next(tmp_path.glob("*.jsonl")).read_text()


def test_abort_trace_requires_process_boundary_and_actual_aborted_free():
    events = []
    for i, name in enumerate(
        [
            "request_received",
            "ipc_submit",
            "core_received",
            "request_queued",
            "http_chunk",
            "abort_sent",
            "abort_received",
            "request_freed",
        ]
    ):
        api = name in {"request_received", "http_chunk"}
        events.append(
            event(
                name,
                "cmpl-r" if api else "cmpl-r-0",
                ts_ns=i,
                pid=(
                    11
                    if name in {"request_received", "ipc_submit", "http_chunk", "abort_sent"}
                    else 22
                ),
                finish_reason="FINISHED_ABORTED",
            )
        )
    assert validate_path(events, "r", "abort")
    events[-1]["finish_reason"] = "FINISHED_LENGTH_CAPPED"
    with pytest.raises(ValueError, match="raced"):
        validate_path(events, "r", "abort")


def test_partial_jsonl_is_never_silently_ignored(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(json.dumps(event("request_queued", state="WAITING")) + '\n{"partial":')
    with pytest.raises(ValueError):
        load_events([path])
