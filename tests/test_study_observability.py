import copy
import json
import math
from urllib.parse import parse_qs, urlsplit

import pytest

from src.analyze_week05 import capacity_brackets, queue_growth, slo_attained
from src.capture_run_metrics import capture, inventory, render_queries
from src.common import load_yaml
from src.study_contract import serve_command, validate_week05
from src.study_runner import prepare_jobs, request_record


def test_slo_requires_success_all_dimensions_and_finite_values():
    slo = {"ttft_ms": 500, "tpot_ms": 50, "e2e_ms": 2500}
    row = {"status": "success", **slo}
    assert slo_attained(row, slo)
    for override in (
        {"status": "timeout"},
        {"tpot_ms": 51},
        {"ttft_ms": math.nan},
        {"ttft_ms": -1},
        {"e2e_ms": None},
    ):
        assert not slo_attained({**row, **override}, slo)


def test_queue_growth_uses_second_half_only():
    samples = [(0, 100), (1, 50), (2, 0), (3, 1), (4, 2), (5, 3), (6, 4)]
    assert queue_growth(samples, 0, 6) == pytest.approx(1)
    with pytest.raises(ValueError):
        queue_growth(samples[:2], 0, 6)


def runs(rate, outcomes, valid=True):
    return [
        {
            "mixture": "mixed",
            "offered_rps": rate,
            "repeat": i,
            "valid": valid,
            "stable": stable,
            "run_id": f"{rate}-{i}",
        }
        for i, stable in enumerate(outcomes)
    ]


def test_capacity_requires_three_agreeing_runs_and_stops_at_first_gap():
    data = runs(1, [True] * 3) + runs(2, [True, False, True]) + runs(3, [False] * 3)
    bracket = capacity_brackets(data, 3)["mixed"]
    assert bracket["max_stable_rps"] == 1
    assert bracket["first_unstable_rps"] == 3
    assert bracket["points"][1]["state"] == "inconsistent"
    assert capacity_brackets(runs(1, [True, True]), 3)["mixed"]["max_stable_rps"] is None
    assert capacity_brackets(runs(1, [True] * 3, False), 3)["mixed"]["max_stable_rps"] is None


def exposition(profile):
    return "\n".join(
        f'{r["metric"]}{{model_name="model",engine="0"}} 0' for r in profile["queries"].values()
    )


def test_metric_inventory_and_scoped_queries():
    profile = load_yaml("observability/prometheus/queries.yaml")
    text = exposition(profile)
    discovered = inventory(text)
    assert discovered["vllm:num_requests_waiting"]["labels"] == ["engine", "model_name"]
    rendered = render_queries(profile, discovered, {"job": "vllm", "instance": "127.0.0.1:8000"}, 5)
    assert rendered["waiting"]["expression"] == (
        'sum(vllm:num_requests_waiting{instance="127.0.0.1:8000",job="vllm"})'
    )
    assert "[20s]" in rendered["queue_p99"]["expression"]
    assert rendered["queue_p99"]["offset_seconds"] == 20
    with pytest.raises(ValueError, match="absent"):
        render_queries(profile, {}, {"job": "vllm", "instance": "host:8000"}, 5)


def fake_prometheus(url, *, reset=False, stale=False):
    query = parse_qs(urlsplit(url).query)
    start, end = float(query["start"][0]), float(query["end"][0])
    times = range(int(start), int(end) + 1, 5)
    expr = query["query"][0]
    values = (
        [[t, str(t - 100 if stale else t)] for t in times]
        if "timestamp(" in expr
        else [[t, str(t if "_total{" in expr else 0.1)] for t in times]
    )
    if reset and "generation_tokens_total" in expr:
        values[-1][1] = "0"
    return {
        "status": "success",
        "data": {"resultType": "matrix", "result": [{"metric": {}, "values": values}]},
    }


@pytest.mark.parametrize("problem", ["reset", "stale"])
def test_capture_rejects_counter_resets_and_stale_scrapes(problem):
    config = load_yaml("configs/week05.yaml")
    profile = load_yaml(config["observability"]["queries"])
    metadata = {"run_id": "test", "measurement_start": 100, "measurement_end": 160}
    good = capture(config["observability"], metadata, exposition(profile), fetch=fake_prometheus)
    assert good["errors"] == []
    bad = capture(
        config["observability"],
        metadata,
        exposition(profile),
        fetch=lambda url: fake_prometheus(url, **{problem: True}),
    )
    assert bad["errors"]
    assert bad["run_id"] == "test"


def test_draft_config_is_plannable_but_cannot_claim_measured_calibration():
    config = load_yaml("configs/week05.yaml")
    base = validate_week05(config)
    assert "--no-enable-prefix-caching" in serve_command(base)
    with pytest.raises(ValueError, match="calibrate"):
        validate_week05(config, execution=True)
    changed = copy.deepcopy(config)
    changed["mixtures"]["mixed"]["short_chat"] = 0.9
    with pytest.raises(ValueError, match="sum to one"):
        validate_week05(changed)


class Tokenizer:
    def encode(self, text, **kwargs):
        return [10, 20, 30]


def test_open_loop_reuses_shape_prefix_when_only_rate_changes():
    config = load_yaml("configs/week05.yaml")
    low = prepare_jobs(config, "mixed", 1, 0, Tokenizer())
    high = prepare_jobs(config, "mixed", 2, 0, Tokenizer())
    for a, b in zip(low, high):
        assert a["offset"] == pytest.approx(2 * b["offset"])
        assert a["workload"] == b["workload"]
        assert a["prompt_ids"] == b["prompt_ids"]


def test_client_tpot_stops_at_last_content_not_final_usage(monkeypatch):
    from src import study_runner
    from src.openai_stream import SSEEvent

    clock = {"time": 10.0}
    monkeypatch.setattr(study_runner.time, "monotonic", lambda: clock["time"])

    def stream(*args, **kwargs):
        for ts, value in [
            (10.1, {"choices": [{"index": 0, "text": "a"}]}),
            (10.2, {"choices": [{"index": 0, "text": "b", "finish_reason": "length"}]}),
            (11.0, {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 2}}),
        ]:
            clock["time"] = ts
            yield SSEEvent(data=json.dumps(value))
        yield SSEEvent(data="[DONE]")

    monkeypatch.setattr(study_runner, "stream_sse", stream)
    job = {
        "request_id": "r",
        "offset": 0,
        "prompt_ids": [1, 2],
        "output_tokens": 2,
        "workload": "short_chat",
    }
    row = request_record("http://127.0.0.1:8000", "model", job, 100, 10, 5, 42)
    assert row["status"] == "success"
    assert row["tpot_ms"] == pytest.approx(100)
    assert row["e2e_ms"] == pytest.approx(1000)
