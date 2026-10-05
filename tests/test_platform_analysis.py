import pytest

from src.analyze_platform import (
    IDENTITY_FIELDS,
    allocation_cost,
    cold_start,
    deletion_audit,
    epp_analysis,
    metric_value,
    prefix_analysis,
    resource_graph,
)


def test_fail_close_needs_complete_dispatch_evidence():
    clients = [dict(request_id="r1", status="http_error")]
    assert (
        epp_analysis(clients, [], [], fault=True)["rows"][0]["status"] == "incomplete"
    )
    assert (
        epp_analysis(clients, [], [], fault=True, complete_dispatch_window=True)[
            "rows"
        ][0]["status"]
        == "closed"
    )
    hops = [dict(request_id="r1", pod_uid="p1")]
    assert (
        epp_analysis(clients, [], hops, fault=True)["rows"][0]["status"]
        == "unexpected_dispatch_or_success"
    )


def test_epp_selection_is_not_successful_dispatch():
    clients = [dict(request_id="r1", status="success")]
    decisions = [
        dict(request_id="r1", selected_pod_uid="p1", ready_candidate_uids=["p1"])
    ]
    assert epp_analysis(clients, decisions, [dict(request_id="r1", pod_uid="p2")])[
        "counts"
    ] == {"mismatch": 1}
    assert epp_analysis(clients, decisions, [dict(request_id="r1", pod_uid="p1")])[
        "counts"
    ] == {"matched": 1}
    assert epp_analysis(clients, decisions, [dict(request_id="r1", pod_uid="p1")] * 2)[
        "counts"
    ] == {"multiple_attempts": 1}


def test_prediction_cannot_substitute_for_actual_reuse():
    identity = {k: "fixed" for k in IDENTITY_FIELDS}
    decision = dict(
        request_id="r1",
        selected_pod_uid="fixed",
        identity=identity,
        predicted_tokens=512,
    )
    assert prefix_analysis([decision], [])[0]["actual_reused_tokens"] is None
    reuse = dict(request_id="r1", identity=dict(identity), reused_tokens=0)
    assert prefix_analysis([decision], [reuse])[0]["actual_reused_tokens"] == 0
    reuse["identity"]["process_generation"] = "new"
    assert prefix_analysis([decision], [reuse])[0]["status"] == "identity_mismatch"


def obj(uid, kind, owners=()):
    return dict(
        kind=kind,
        metadata=dict(
            uid=uid,
            name=uid,
            namespace="lab",
            ownerReferences=[dict(uid=u) for u in owners],
        ),
    )


def test_resource_graph_uses_uids_and_transitive_ownership():
    before = [
        obj("parent", "LLMInferenceService"),
        obj("dep", "Deployment", ["parent"]),
        obj("rs", "ReplicaSet", ["dep"]),
        obj("pod", "Pod", ["rs"]),
        obj("shared", "Gateway"),
    ]
    after = [before[-1], obj("newpod", "Pod")]
    graph = resource_graph(before)
    assert len(graph["edges"]) == 3
    verdict = deletion_audit(before, after, "parent")
    assert verdict["remaining_owned_uids"] == []
    assert verdict["removed_unowned_uids"] == []
    assert verdict["removed_owned_uids"] == ["dep", "parent", "pod", "rs"]


@pytest.mark.parametrize(
    "values",
    [
        [],
        [[99, "NaN"]],
        [[99, "Inf"]],
        [[1, "2"]],
        [[99, "1"], [99, "2"]],
        [[101, "1"]],
    ],
)
def test_metric_unknown_is_never_zero(values):
    response = dict(
        status="success",
        data=dict(resultType="vector", result=[dict(value=v) for v in values]),
    )
    with pytest.raises(ValueError):
        metric_value(response, 100, 30)


def test_explicit_zero_is_valid():
    response = dict(
        status="success", data=dict(resultType="vector", result=[dict(value=[99, "0"])])
    )
    assert metric_value(response, 100, 30) == 0


def test_allocated_hours_do_not_imply_bill_savings():
    rows = [dict(timestamp=0, allocated_gpus=2), dict(timestamp=1800, allocated_gpus=1)]
    result = allocation_cost(rows, 3600, max_gap=1800)
    assert result["allocated_gpu_hours"] == 1.5
    assert result["billed_gpu_hours"] is None
    result = allocation_cost(
        rows,
        3600,
        max_gap=1800,
        billed=dict(
            start=0,
            end=3600,
            source="provider",
            billed_gpu_hours=2,
            billed_node_hours=2,
        ),
    )
    assert result["billed_gpu_hours"] == 2
    with pytest.raises(ValueError, match="gap"):
        allocation_cost(rows, 3600, max_gap=30)


def test_cold_start_requires_full_timeline():
    assert (
        cold_start([dict(pod_uid="p1", observed=1, desired=2)])[0]["status"]
        == "incomplete"
    )
    row = dict(
        pod_uid="p1",
        observed=1,
        desired=2,
        scheduled=4,
        model_ready=10,
        route_eligible=11,
        first_token=12,
    )
    assert cold_start([row])[0]["durations_seconds"]["scheduled_to_model_ready"] == 6
