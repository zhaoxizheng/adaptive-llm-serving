import copy
import json
import time

import pytest

from src.analyze_routing import attribution, gpu_seconds, rr_verdict
from src.cluster_contract import current_conditions, gateway_resources, rr_resources, validate_lock, verify_rank_evidence, weight_verdict
from src.common import load_yaml
from src.serving_experiment import counter_delta, engine_command, load_experiment, summarize, trace_jobs
from src.summarize_ncu import parse_counters


class Tokenizer:
    all_special_ids = [0]

    def get_vocab(self):
        return {str(i): i for i in range(2000)}


def test_paired_trace_and_disjoint_model_warmup():
    cfg, _ = load_experiment("configs/week13-prefix.yaml")
    a = trace_jobs(cfg, Tokenizer(), "shared", 2, 0)
    b = trace_jobs(cfg, Tokenizer(), "shared", 2, 0)
    warm = trace_jobs(cfg, Tokenizer(), "short", 2, 0, warmup=True)
    low = trace_jobs(cfg, Tokenizer(), "shared", 2, 0, low_overlap=True)
    assert a == b
    assert a != trace_jobs(cfg, Tokenizer(), "shared", 2, 1)
    assert a[0]["prompt_ids"][:512] == a[4]["prompt_ids"][:512]
    assert len(a[0]["prompt_ids"]) == 768
    assert len({j["prompt_ids"][0] for j in low}) == cfg["requests"]
    assert {j["prompt_ids"][0] for j in warm}.isdisjoint(j["prompt_ids"][0] for j in a + low)
    assert [j["offset"] for j in a] == [j["offset"] for j in low]


def test_failures_stay_in_goodput_and_slo_denominator():
    records = [dict(status="success", ttft_ms=50, tpot_ms=10, output_tokens=10),
               dict(status="timeout", ttft_ms=None, tpot_ms=None, output_tokens=10),
               dict(status="success", ttft_ms=300, tpot_ms=10, output_tokens=10)]
    summary = summarize(records, {"ttft_ms": 100, "tpot_ms": 20}, 2)
    assert summary["goodput_rps"] == .5
    assert summary["slo_attainment"] == 1 / 3
    assert summary["errors"] == 1
    assert summary["p99_exploratory"]


def test_counter_reset_is_not_hidden_by_other_series_growth():
    before = 'hits{rank="0"} 100\nhits{rank="1"} 100\n'
    after = 'hits{rank="0"} 1\nhits{rank="1"} 300\n'
    assert counter_delta(before, after, "hits")["status"] == "reset"
    assert counter_delta("hits 1", "hits 3", "hits")["value"] == 2
    assert counter_delta("", "", "hits")["status"] == "unavailable"


def test_counter_csv_keeps_launch_units_and_non_numeric_evidence():
    rows = parse_counters('==PROF== message\n"ID","Kernel Name","Metric Name","Metric Unit","Metric Value"\n"0","attention","duration","nsecond","1,234"\n"1","attention","occupancy","%","N/A"\n')
    assert rows[0]["value"] == 1234
    assert rows[0]["unit"] == "nsecond"
    assert rows[1]["raw_value"] == "N/A" and rows[1]["value"] is None
    with pytest.raises(ValueError):
        parse_counters("ERR_NVGPUCTRPERM")


def test_manifests_preserve_gpu_shape_and_do_not_create_public_services():
    base = load_yaml("configs/serving-baseline.yaml")
    lock = load_yaml("configs/cluster-lab.yaml")
    base["gpus_per_replica"] = base["engine"]["tensor_parallel_size"] = 2
    resources = rr_resources(base, lock) + gateway_resources(base, lock)
    for resource in resources:
        if resource["kind"] == "Service":
            assert resource["spec"]["type"] == "ClusterIP"
        if resource["kind"] == "Deployment" and resource["metadata"]["name"].startswith("vllm"):
            pod = resource["spec"]["template"]["spec"]
            engine = pod["containers"][0]
            assert engine["resources"]["limits"]["nvidia.com/gpu"] == 2
            assert engine["args"][engine["args"].index("--tensor-parallel-size") + 1] == "2"
            assert resource["spec"]["strategy"]["rollingUpdate"]["maxSurge"] == 0
    route = next(r for r in resources if r["kind"] == "HTTPRoute")
    headers = [r["matches"][0]["headers"][0]["value"] for r in route["spec"]["rules"]]
    assert len(headers) == len(set(headers))
    with pytest.raises(ValueError, match="freeze"):
        validate_lock(lock, base, 16)


def test_source_override_and_frozen_shape_checks():
    _, base = load_experiment("configs/week14-optimization.yaml")
    with pytest.raises(ValueError, match="uncontrolled"):
        engine_command(base, {"enable_lora": True})
    argv = engine_command(base, {"enforce_eager": True})
    assert "--enforce-eager" in argv


def test_stale_or_wrong_parent_conditions_fail():
    resource = dict(metadata=dict(generation=3, namespace="lab"), status=dict(parents=[
        dict(parentRef=dict(name="inference"), conditions=[dict(type="Accepted", status="True", observedGeneration=2)])]))
    assert not current_conditions(resource, ["Accepted"], parent="inference")
    resource["status"]["parents"][0]["conditions"][0]["observedGeneration"] = 3
    assert current_conditions(resource, ["Accepted"], parent="inference")
    assert not current_conditions(resource, ["Accepted"], parent="other")


def test_rank_evidence_rejects_cross_node_and_duplicate_gpu():
    pod = dict(metadata=dict(uid="p"), spec=dict(nodeName="n", containers=[dict(name="vllm", resources=dict(limits={"nvidia.com/gpu": 2}))]))
    evidence = dict(pod_uid="p", node="n", tensor_parallel_size=2,
                    ranks=[dict(local_rank=0, gpu_uuid="g0", node="n"), dict(local_rank=1, gpu_uuid="g1", node="n")])
    assert verify_rank_evidence(pod, evidence, 2)
    evidence["ranks"][1]["node"] = "other"
    with pytest.raises(ValueError, match="spans"):
        verify_rank_evidence(pod, evidence, 2)


def test_weights_require_samples_and_keep_zero_weight_exact():
    assert weight_verdict(90, 10, [90, 10])["status"] == "insufficient_samples"
    assert weight_verdict(900, 100, [90, 10])["status"] == "compatible"
    assert weight_verdict(500, 500, [90, 10])["status"] == "outside_interval"
    assert weight_verdict(999, 1, [100, 0])["status"] == "outside_interval"


def test_rr_attribution_checks_order_and_keeps_retries():
    rows = [dict(event="request", request_id=str(i), gateway_id="g", epoch=1,
                 sequence=i, ready_uids=["a", "b"], pod_uid="ab"[i % 2], upstream="ab"[i % 2]) for i in range(8)]
    assert rr_verdict(list(reversed(rows)))["status"] == "passed"
    corrupt = copy.deepcopy(rows)
    corrupt[4]["pod_uid"] = "b"
    assert rr_verdict(corrupt)["status"] == "failed"
    clients = [dict(request_id="0", status="success", output_tokens=3)]
    result = attribution(clients, rows + [rows[0]])
    assert result[0]["upstream_attempts"] == 2


def test_gpu_cost_integrates_allocations_not_desired_replicas():
    pod = dict(metadata=dict(labels={"serving-study-role": "engine"}),
               spec=dict(nodeName="n", containers=[dict(resources=dict(limits={"nvidia.com/gpu": 2}))]), status=dict(phase="Running"))
    samples = [dict(timestamp=0, pods=dict(items=[pod])), dict(timestamp=10, pods=dict(items=[pod, pod]))]
    assert gpu_seconds(samples, 20) == 60


def test_bounded_load_records_client_overload_instead_of_hidden_queue(tmp_path, monkeypatch):
    from src import experiment_client
    def slow_request(url, model, job, wall, mono, timeout, seed, **kwargs):
        time.sleep(.05)
        return dict(request_id=job["request_id"], status="success", output_tokens=2,
                    workload="short", ttft_ms=10, tpot_ms=1, completed_at=time.time())
    monkeypatch.setattr(experiment_client, "request", slow_request)
    jobs = [dict(request_id=str(i), workload="short", prompt_ids=[1, 2], output_tokens=2, offset=0) for i in range(5)]
    journal = tmp_path / "client.jsonl"
    rows, _ = experiment_client.run_load("http://unused", "model", jobs,
        dict(max_inflight=1, timeout_seconds=1, seed=42), journal)
    assert len(rows) == 5
    assert sum(r["status"] == "client_overload" for r in rows) == 4
    assert len([json.loads(line) for line in journal.read_text().splitlines()]) == 5


def test_live_gate_rejects_single_case_actually_running_two_replicas():
    from scripts.run_cluster_study import status_gate
    class Kube:
        def run(self, *args, **kwargs):
            return {"items": [dict(metadata=dict(name="vllm"), spec=dict(replicas=2), status=dict(readyReplicas=2))]}
    with pytest.raises(ValueError, match="replica count"):
        status_gate(Kube(), 15, {}, case=dict(replicas=1))


def test_kernel_capture_rejects_unbounded_or_external_application_replay():
    from pathlib import Path
    from scripts.run_kernel_study import ncu_command
    cfg = load_yaml("configs/week13-kernel.yaml")
    cfg["ncu"]["launch_count"] = 100
    with pytest.raises(ValueError, match="1-16"):
        ncu_command(cfg, Path("unused"))
    cfg["ncu"]["launch_count"] = 1
    cfg["ncu"]["replay_mode"] = "application"
    with pytest.raises(ValueError, match="externally driven"):
        ncu_command(cfg, Path("unused"))
