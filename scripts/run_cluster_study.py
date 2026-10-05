"""Explicit lab-only render/apply, status, lifecycle, and request matrix entry point."""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
import uuid
from pathlib import Path

import yaml

from src.cluster_contract import current_conditions, gateway_resources, negative_route, rr_resources, template_bundle, validate_lock
from src.common import load_yaml, source_identity, write_json, write_text
from src.experiment_client import request, run_load
from src.serving_experiment import load_experiment, summarize, trace_jobs
from src.study_contract import fingerprint
from src.study_runner import load_tokenizer


class Kubectl:
    def __init__(self, lock):
        self.lock = lock

    def run(self, *args, json_output=False):
        command = ["kubectl", "--context", self.lock["context"], "--namespace", self.lock["namespace"],
                   "--request-timeout=20s", *args]
        if json_output:
            command += ["-o", "json"]
        result = subprocess.check_output(command, text=True, timeout=45)
        return json.loads(result) if json_output else result


def snapshot(kube, root):
    root.mkdir(parents=True, exist_ok=True)
    for kind in ("pods", "services", "deployments", "endpointslices", "hpa", "events"):
        try:
            write_json(root / f"{kind}.json", kube.run("get", kind, json_output=True))
        except subprocess.CalledProcessError as error:
            write_json(root / f"{kind}-error.json", dict(error_type=type(error).__name__))
    pods = kube.run("get", "pods", "-l", "app.kubernetes.io/part-of=serving-study", json_output=True)
    try:
        write_text(root / "container-resources.txt", kube.run("top", "pods", "--containers"))
    except subprocess.SubprocessError as error:
        write_json(root / "container-resources-error.json", dict(error_type=type(error).__name__))
    for pod in pods["items"]:
        for container in pod["spec"]["containers"]:
            name = container["name"]
            if name in {"identity", "gateway", "vllm"}:
                try:
                    write_text(root / f"{pod['metadata']['uid']}-{name}.jsonl",
                               kube.run("logs", pod["metadata"]["name"], "-c", name, "--tail=20000"))
                except subprocess.CalledProcessError:
                    write_json(root / f"{pod['metadata']['uid']}-{name}-error.json", dict(status="log_unavailable"))
        if pod["metadata"].get("labels", {}).get("serving-study-role") == "engine":
            for label, container, command in (
                    ("metrics", "identity", ["python", "-c", "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/metrics', timeout=5).read().decode())"]),
                    ("gpu", "vllm", ["nvidia-smi", "--query-gpu=name,uuid,driver_version,memory.total", "--format=csv"])):
                try:
                    write_text(root / f"{pod['metadata']['uid']}-{label}.txt", kube.run("exec", pod["metadata"]["name"], "-c", container, "--", *command))
                except subprocess.SubprocessError as error:
                    write_json(root / f"{pod['metadata']['uid']}-{label}-error.json", dict(error_type=type(error).__name__))


def status_gate(kube, week, lock, *, case=None, base=None):
    deployments = kube.run("get", "deployments", "-l", "app.kubernetes.io/part-of=serving-study", json_output=True)
    expected = {"vllm-a": 1, "vllm-b": 1} if week == 16 else ({"vllm": case["replicas"]} if case and "replicas" in case else {})
    for name, count in expected.items():
        items = [d for d in deployments["items"] if d["metadata"]["name"] == name]
        if len(items) != 1 or items[0]["spec"]["replicas"] != count or items[0].get("status", {}).get("readyReplicas", 0) != count:
            raise ValueError("actual Ready replica count does not match the selected cell")
    if base:
        engines = [d for d in deployments["items"] if d["metadata"]["name"] in {"vllm", "vllm-a", "vllm-b"}]
        allowed = {"vllm-a", "vllm-b"} if week == 16 else {"vllm"}
        if {d["metadata"]["name"] for d in engines} != allowed:
            raise ValueError("remove other week replica deployments before measurement")
        for deployment in engines:
            container = next(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "vllm")
            if container["image"] != base["image"] or int(container["resources"]["limits"]["nvidia.com/gpu"]) != base["gpus_per_replica"]:
                raise ValueError("live image/GPU resource shape differs from frozen baseline")
            from src.serving_experiment import engine_command
            if container["args"] != engine_command(base)[1:]:
                raise ValueError("live engine arguments drifted from frozen baseline")
    hpas = kube.run("get", "hpa", json_output=True)
    experiment_hpas = [h for h in hpas["items"] if h["spec"]["scaleTargetRef"]["name"] in {"vllm", "vllm-a", "vllm-b"}]
    if case and "min_replicas" in case:
        if len(experiment_hpas) != 1 or experiment_hpas[0]["spec"]["minReplicas"] != 1 or experiment_hpas[0]["spec"]["maxReplicas"] != 2:
            raise ValueError("HPA cell requires exactly the bounded 1-2 CPU baseline")
    elif experiment_hpas:
        raise ValueError("disable the experiment HPA before fixed replica measurement")
    if week == 16:
        hpas = kube.run("get", "hpa", json_output=True)
        if any(h["spec"]["scaleTargetRef"]["name"] in {"vllm", "vllm-a", "vllm-b"} for h in hpas["items"]):
            raise ValueError("disable the experiment HPA before Week 16")
        for kind, name, required, parent in (("gatewayclass", lock["gateway_api"]["class_name"], ["Accepted"], None),
                ("gateway", "inference", ["Accepted", "Programmed"], None),
                ("httproute", "inference", ["Accepted", "ResolvedRefs"], "inference")):
            resource = kube.run("get", kind, name, json_output=True)
            if not current_conditions(resource, required, parent=parent):
                raise ValueError(f"{kind}/{name} has absent, stale or False conditions")


def timeline(kube, root, stop):
    with (root / "timeline.jsonl").open("x") as handle:
        while not stop.is_set():
            row = {"timestamp": time.time()}
            for kind in ("pods", "deployments", "hpa"):
                try:
                    row[kind] = kube.run("get", kind, json_output=True)
                except subprocess.SubprocessError as error:
                    row[kind] = dict(error_type=type(error).__name__)
            handle.write(json.dumps(row) + "\n")
            handle.flush()
            stop.wait(2)


def benchmark(cfg, base, lock, kube, args, root):
    case = cfg["cases"][args.case]
    rate = cfg["rates"][args.rate] or base.get("near_slo_rps")
    if not rate:
        raise ValueError("freeze measured near-SLO rate first")
    status_gate(kube, cfg["week"], lock, case=case, base=base)
    tokenizer = load_tokenizer(base)
    workloads = [args.workload] if args.workload else case.get("workloads", cfg["workloads"])
    if any(w not in cfg["workloads"] for w in workloads):
        raise ValueError("unknown workload")
    model = base["model"]["served_model_name"]
    endpoint = case.get("endpoint", cfg["endpoint"])
    if "path" in case:
        from urllib.parse import urlsplit, urlunsplit
        parsed = urlsplit(endpoint)
        endpoint = urlunsplit((parsed.scheme, parsed.netloc, case["path"], "", ""))
    headers = {}
    if cfg["week"] == 16 and args.case != "direct":
        headers = {"Host": case.get("hostname", cfg["hostname"]), "X-Lab-Case": case.get("header", "only-a")}
    for repeat in range(cfg["repeats"]):
        for workload in workloads:
            cell = root / f"r{repeat}-{workload}"
            cell.mkdir()
            jobs = trace_jobs(cfg, tokenizer, workload, rate, repeat)
            trace_id = fingerprint(jobs)
            jobs = [{**j, "request_id": root.name[:12] + f"-{repeat}-{workload}-" + j["request_id"]} for j in jobs]
            write_json(cell / "trace.json", jobs)
            if args.cache_state == "unknown":
                raise ValueError("set an explicit cache-state; cold/warm resets owned deployments per repeat")
            if args.cache_state in {"cold", "warm"}:
                names = ["vllm-a", "vllm-b"] if cfg["week"] == 16 else ["vllm"]
                for name in names:
                    kube.run("rollout", "restart", "deployment/" + name)
                # Poll readiness with bounded kubectl calls; cold-start events remain in timeline.
                deadline = time.monotonic() + 900
                while True:
                    deployments = kube.run("get", "deployments", json_output=True)["items"]
                    ready = all(any(d["metadata"]["name"] == name
                        and d.get("status", {}).get("observedGeneration", 0) >= d["metadata"]["generation"]
                        and d.get("status", {}).get("updatedReplicas", 0) == d["spec"]["replicas"]
                        and d.get("status", {}).get("availableReplicas", 0) == d["spec"]["replicas"]
                        and d.get("status", {}).get("replicas", 0) == d["spec"]["replicas"]
                        for d in deployments) for name in names)
                    if ready:
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError("replicas did not finish cold-start rollout")
                    time.sleep(2)
                warmups = trace_jobs(cfg, tokenizer, "short", rate, repeat, warmup=True)
                primers = []
                if args.cache_state == "warm":
                    primers = [{**j, "prompt_ids": j["prompt_ids"][:cfg["shared_tokens"]], "output_tokens": 1}
                               for j in trace_jobs(cfg, tokenizer, "shared", rate, repeat)[:cfg["prefix_families"]]]
                pods = kube.run("get", "pods", "-l", "serving-study-role=engine", json_output=True)["items"]
                evidence = []
                # Run untimed model warmup and generation smoke directly in every owned Pod.
                code = "import json,sys,urllib.request; p=json.loads(sys.argv[1]); r=urllib.request.Request('http://127.0.0.1:8000/v1/completions', data=json.dumps(p).encode(), headers={'Content-Type':'application/json'}); print(urllib.request.urlopen(r, timeout=30).read().decode())"
                for pod in pods:
                    if pod["metadata"].get("deletionTimestamp"):
                        continue
                    for index, job in enumerate(warmups + primers):
                        payload = dict(model=model, prompt=job["prompt_ids"], max_tokens=job["output_tokens"],
                                       temperature=0, ignore_eos=True, stream=False,
                                       request_id=f"warm-{root.name[:8]}-{repeat}-{index}")
                        result = json.loads(kube.run("exec", pod["metadata"]["name"], "-c", "identity", "--",
                                                     "python", "-c", code, json.dumps(payload)))
                        evidence.append(dict(pod_uid=pod["metadata"]["uid"], kind="model" if index < len(warmups) else "prefix", usage=result.get("usage")))
                        if result.get("usage", {}).get("completion_tokens") != job["output_tokens"]:
                            raise ValueError("per-replica generation warmup failed")
                write_json(cell / "warmup.json", evidence)
                status_gate(kube, cfg["week"], lock, case=case, base=base)
            meta = dict(status="running", workload_id=trace_id, config=cfg, baseline=base,
                        case=args.case, repeat=repeat, workload=workload, cache_state=args.cache_state,
                        source=source_identity(), hardware_executed=True)
            write_json(cell / "run.json", meta)
            try:
                snapshot(kube, cell / "before")
                # Explicit cache state is recorded, never silently described as fresh.
                rows, window = run_load(endpoint, model, jobs, cfg, cell / "client.jsonl", headers=headers)
                write_json(cell / "summary.json", summarize(rows, base["slo"], window))
                snapshot(kube, cell / "after")
                if args.cancel_smoke and not case.get("expected_failure"):
                    job = {**jobs[0], "offset": 0, "output_tokens": 256, "request_id": jobs[0]["request_id"] + "-cancel"}
                    write_json(cell / "cancel.json", request(endpoint, model, job, time.time(), time.monotonic(),
                        cfg["timeout_seconds"], cfg["seed"], headers=headers, cancel=True))
                if case.get("expected_failure"):
                    # Runtime failures need independent zero-backend-hit/status evidence.
                    meta["status"] = "negative_observed_pending_attribution" if all(r["status"] == "http_error" for r in rows) else "rejected"
                else:
                    meta["status"] = "collected_pending_attribution" if all(r["status"] == "success" for r in rows) else "rejected"
            except BaseException as error:
                meta.update(status="failed", error_type=type(error).__name__)
                raise
            finally:
                write_json(cell / "run.json", meta)
            if meta["status"] == "rejected":
                raise ValueError("client evidence rejected; inspect saved raw results")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--lock")
    parser.add_argument("--case", default="rr")
    parser.add_argument("--rate", default="low")
    parser.add_argument("--workload")
    parser.add_argument("--output")
    parser.add_argument("--cache-state", choices=["cold", "warm", "mixed", "unknown"], default="unknown")
    parser.add_argument("--cancel-smoke", action="store_true")
    parser.add_argument("--pod")
    parser.add_argument("--deployment", default="vllm")
    parser.add_argument("--observe-seconds", type=int, default=180)
    group = parser.add_mutually_exclusive_group(required=True)
    for flag in ("plan", "render", "preflight", "server-dry-run", "apply", "snapshot", "run", "rollout", "fail-pod"):
        group.add_argument("--" + flag, action="store_true")
    args = parser.parse_args()
    if not 1 <= args.observe_seconds <= 600:
        parser.error("observe-seconds must be 1-600")
    cfg, base = load_experiment(args.config)
    lock = load_yaml(args.lock or cfg["cluster_lock"])
    if args.plan:
        print(json.dumps(dict(week=cfg["week"], cases=cfg["cases"], replica_shape=base["gpus_per_replica"],
                              total_gpu_slots=2 * base["gpus_per_replica"], lock=lock, hardware_executed=False), indent=2))
        return
    if args.render:
        if not args.output:
            parser.error("--render requires --output")
        # Template rendering is available without credentials or a live cluster.
        text = "# TEMPLATE ONLY: fill immutable image/version locks before apply.\n" + yaml.safe_dump_all(template_bundle(base, lock, cfg["week"]), sort_keys=False)
        write_text(Path(args.output), text)
        return
    validate_lock(lock, base, cfg["week"])
    kube = Kubectl(lock)
    if args.preflight:
        root = Path(args.output or cfg["output_root"]) / ("preflight-" + uuid.uuid4().hex)
        write_json(root / "version.json", kube.run("version", json_output=True))
        for kind in ("nodes", "resourcequota", "limitrange"):
            write_json(root / f"{kind}.json", kube.run("get", kind, json_output=True))
        snapshot(kube, root)
        print(root)
        return
    if args.apply or args.server_dry_run:
        if args.case not in cfg["cases"]:
            parser.error("select a case from the configured matrix")
        case = cfg["cases"][args.case]
        existing = kube.run("get", "deployments", "-l", "app.kubernetes.io/part-of=serving-study", json_output=True)
        incompatible = {"vllm"} if cfg["week"] == 16 else {"vllm-a", "vllm-b", "vllm-tp2"}
        if any(d["metadata"]["name"] in incompatible and d["spec"].get("replicas", 1) > 0 for d in existing["items"]):
            raise ValueError("remove incompatible lab replicas before allocating this week's GPU budget")
        resources = gateway_resources(base, lock) if cfg["week"] == 16 else rr_resources(base, lock,
            replicas=case.get("replicas", 1), hpa=args.case == "hpa_burst")
        if cfg["week"] == 16 and args.case in {"invalid_backend", "wrong_port"}:
            resources.append(negative_route(lock, args.case))
        # Switching from HPA to fixed replicas is explicit; reject competing ownership.
        hpas = kube.run("get", "hpa", json_output=True)
        if args.case != "hpa_burst" and any(h["spec"]["scaleTargetRef"]["name"] in {"vllm", "vllm-a", "vllm-b"} for h in hpas["items"]):
            raise ValueError("remove the experiment HPA before applying a fixed-replica cell")
        root = Path(args.output or cfg["output_root"]) / ("render-" + uuid.uuid4().hex)
        filename = root / "manifests.yaml"
        write_text(filename, yaml.safe_dump_all(resources, sort_keys=False))
        extra = ["--dry-run=server"] if args.server_dry_run else []
        write_text(root / "apply.txt", kube.run("apply", *extra, "-f", str(filename)))
        print(root)
        return
    root = Path(args.output or cfg["output_root"]) / uuid.uuid4().hex
    root.mkdir(parents=True)
    session_meta = dict(config=cfg, lock=lock, source=source_identity(), status="started")
    write_json(root / "session.json", session_meta)
    snapshot(kube, root / "before")
    stop = threading.Event()
    monitor = threading.Thread(target=timeline, args=(kube, root, stop), daemon=True)
    monitor.start()
    try:
        if cfg["week"] == 16:
            for kind in ("gatewayclass", "gateway", "httproute"):
                write_json(root / f"{kind}-before.json", kube.run("get", kind, json_output=True))
        if args.run:
            if args.case not in cfg["cases"]:
                parser.error("select a configured case")
            benchmark(cfg, base, lock, kube, args, root)
        elif args.rollout or args.fail_pod:
            kind, name = ("pod", args.pod) if args.fail_pod else ("deployment", args.deployment)
            if not name:
                parser.error("--fail-pod requires --pod")
            resource = kube.run("get", kind, name, json_output=True)
            if resource["metadata"].get("labels", {}).get("app.kubernetes.io/part-of") != "serving-study":
                raise ValueError("lifecycle action target is not owned by this lab")
            if args.fail_pod:
                command = ["delete", "pod", name, "--grace-period=0", "--force", "--wait=false"]
            else:
                command = ["rollout", "restart", "deployment/" + name]
            write_json(root / "action.json", dict(timestamp=time.time(), command=command))
            write_text(root / "action.txt", kube.run(*command))
            stop.wait(args.observe_seconds)
        if cfg["week"] == 16:
            for kind in ("gatewayclass", "gateway", "httproute"):
                write_json(root / f"{kind}.json", kube.run("get", kind, json_output=True))
        session_meta["status"] = "collected_pending_review"
    except BaseException as error:
        session_meta.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        stop.set()
        monitor.join(timeout=150)
        snapshot(kube, root / "after")
        write_json(root / "session.json", session_meta)
    print(root)


if __name__ == "__main__":
    main()
