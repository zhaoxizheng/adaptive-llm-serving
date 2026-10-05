"""Explicit, bounded lab operations for GAIE, llm-d, KServe and HPA/KEDA studies."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid
import urllib.parse
import urllib.request

import yaml

from src.cluster_contract import current_conditions, digest
from src.common import source_identity, write_json, write_text
from src.platform_contract import (
    artifact,
    check_pool_schema,
    crd_schema,
    data_plane,
    documents,
    load_study,
    manifest_images,
    require_subset,
    scaling_resources,
    scoped_resources,
    validate_versions,
    writer_check,
)
from src.platform_workload import make_jobs, matrix, run_requests, trace_metadata
from src.analyze_platform import metric_value


class Cluster:
    def __init__(self, lab, root):
        self.lab, self.root = lab, root
        self.journal_lock = threading.Lock()

    def call(self, *args, parse=True, timeout=45):
        argv = [
            "kubectl",
            "--context",
            self.lab["context"],
            "--namespace",
            self.lab["namespace"],
            "--request-timeout=20s",
            *args,
        ]
        started = time.time()
        result = subprocess.run(
            argv, text=True, capture_output=True, timeout=timeout, check=False
        )
        with self.journal_lock, (self.root / "commands.jsonl").open("a") as handle:
            handle.write(
                json.dumps(
                    dict(
                        argv=argv,
                        started=started,
                        ended=time.time(),
                        returncode=result.returncode,
                    )
                )
                + "\n"
            )
        if result.returncode:
            # Status is persisted; do not dump unreviewed controller response bodies.
            raise RuntimeError(
                f"kubectl failed ({result.returncode}): {' '.join(args[:3])}"
            )
        return json.loads(result.stdout) if parse else result.stdout

    def get(self, kind, name=None):
        return self.call(
            "get",
            kind,
            *([name] if name else []),
            "-o",
            "json",
            "--show-managed-fields=true",
        )

    def available(self):
        return set(
            self.call(
                "api-resources",
                "--namespaced=true",
                "--verbs=list",
                "-o",
                "name",
                parse=False,
            ).split()
        )


def items_if_available(kube, resource, available):
    return kube.get(resource)["items"] if resource in available else []


def snapshot(kube, cfg, directory):
    directory.mkdir(parents=True, exist_ok=True)
    available = kube.available()
    write_json(directory / "discovery.json", sorted(available))
    objects, errors = [], []
    for resource in cfg["audit_resources"]:
        if resource not in available:
            errors.append(dict(resource=resource, status="not_served"))
            continue
        try:
            objects.extend(kube.get(resource)["items"])
        except (RuntimeError, subprocess.TimeoutExpired):
            errors.append(dict(resource=resource, status="unavailable"))
    write_text(
        directory / "objects.jsonl", "".join(json.dumps(o) + "\n" for o in objects)
    )
    write_json(directory / "collection-errors.json", errors)
    return objects


def render(cfg, base, lab, versions, case_name, *, template=False):
    case = cfg["cases"][case_name]
    if cfg["week"] == 19:
        return documents(artifact(versions, case["artifact"]))
    if cfg["week"] == 20:
        return scaling_resources(cfg, lab, case.get("mode", case_name))
    result = data_plane(cfg, base, lab, case)
    if not template:
        result += documents(artifact(versions, "gateway_glue"))
        key = "reference_epp" if cfg["week"] == 17 else case["artifact"]
        result += documents(artifact(versions, key))
    return result


def verify_live_case(kube, cfg, base, versions, case_name):
    expected = render(cfg, base, kube.lab, versions, case_name)
    if cfg["week"] == 20:
        expected += documents(artifact(versions, "llmd_load"))
    for wanted in expected:
        md = wanted["metadata"]
        actual = kube.get(wanted["kind"], md["name"])
        for field in (
            "apiVersion",
            "kind",
            "spec",
            "data",
            "rules",
            "roleRef",
            "subjects",
        ):
            if field in wanted:
                require_subset(
                    wanted[field], actual.get(field), md["name"] + "." + field
                )
        if wanted["kind"] == "InferencePool":
            expected_mode = wanted["spec"]["endpointPickerRef"].get(
                "failureMode", "FailClose"
            )
            actual_mode = actual["spec"]["endpointPickerRef"].get(
                "failureMode", "FailClose"
            )
            if expected_mode != actual_mode:
                raise ValueError(
                    "live InferencePool failure mode differs from this cell"
                )
        if wanted["kind"] == "Deployment":
            status = actual.get("status", {})
            replicas = actual["spec"].get("replicas", 1)
            if (
                status.get("observedGeneration")
                != md.get("generation", actual["metadata"]["generation"])
                or status.get("readyReplicas", 0) != replicas
            ):
                raise ValueError("selected Deployment/EPP configuration is not Ready")


def schema_preflight(kube, cfg, versions, root):
    expected = documents(artifact(versions, "gaie_crds"))
    check_pool_schema(expected)
    all_crds = kube.get("customresourcedefinitions")["items"]
    write_json(root / "installed-crds.json", all_crds)
    groups = [("InferencePool", "v1", expected)]
    if cfg["week"] == 19:
        pinned = documents(artifact(versions, "kserve_crds"))
        for kind in ("LLMInferenceService", "LLMInferenceServiceConfig"):
            groups.append((kind, versions["kserve_api_version"].split("/")[-1], pinned))
    for kind, version, pinned in groups:
        if crd_schema(pinned, kind, version) != crd_schema(all_crds, kind, version):
            raise ValueError(
                f"installed {kind} schema differs from the pinned artifact"
            )
    write_text(
        root / "kubernetes-version.json",
        kube.call("version", "-o", "json", parse=False),
    )
    # Resource existence and current status do not establish controller conformance.
    for kind, name, required in (
        ("gatewayclass", kube.lab["gateway_api"]["class_name"], ["Accepted"]),
        ("gateway", cfg["gateway_name"], ["Accepted", "Programmed"]),
    ):
        obj = kube.get(kind, name)
        write_json(root / f"{kind}.json", obj)
        if not current_conditions(obj, required):
            raise ValueError(f"{kind} has absent, false or stale conditions")


def ownership(kube, cfg, mode):
    available = kube.available()
    dep = kube.get("deployment", cfg["target"])
    hpas = kube.get("horizontalpodautoscalers.autoscaling")["items"]
    scaled = items_if_available(kube, "scaledobjects.keda.sh", available)
    return writer_check(dep, hpas, scaled, mode)


def ready(kube, cfg, base, mode):
    if cfg["week"] == 19:
        parents = kube.get("llminferenceservices.serving.kserve.io")["items"]
        own = [o for o in parents if o["metadata"]["name"] == cfg["llmisvc_name"]]
        if len(own) != 1 or not current_conditions(own[0], ["Ready"]):
            raise ValueError("LLMInferenceService Ready is absent, false or stale")
        return
    mode = cfg["cases"][mode].get("mode", mode)
    ownership(kube, cfg, mode if cfg["week"] == 20 else "fixed_2")
    dep = kube.get("deployment", cfg["target"])
    spec, status = dep["spec"], dep.get("status", {})
    desired = spec["replicas"]
    expected = (
        int(mode[-1]) if mode.startswith("fixed_") else 2 if cfg["week"] != 20 else None
    )
    if expected is not None and desired != expected:
        raise ValueError("fixed replica count differs from the selected cell")
    if (
        desired < 1
        or desired > 2
        or status.get("observedGeneration") != dep["metadata"]["generation"]
        or status.get("updatedReplicas", 0) != desired
        or status.get("readyReplicas", 0) != desired
    ):
        raise ValueError("replica rollout is not Ready at the current generation")
    engine = next(
        c for c in spec["template"]["spec"]["containers"] if c["name"] == "vllm"
    )
    from src.serving_experiment import engine_command

    expected_args = engine_command(base, host="0.0.0.0")[1:]
    resources = engine["resources"]
    if (
        engine["image"] != base["image"]
        or engine["args"] != expected_args
        or any(
            int(resources[k]["nvidia.com/gpu"]) != base["gpus_per_replica"]
            for k in ("requests", "limits")
        )
    ):
        raise ValueError(
            "live engine image/arguments/GPU shape differs from the frozen baseline"
        )
    if any(
        d["metadata"]["name"] in {"vllm-a", "vllm-b"}
        for d in kube.get("deployment")["items"]
    ):
        raise ValueError(
            "remove the Week 16 a/b deployments before the platform experiment"
        )
    route = kube.get("httproute", "inference-platform")
    if not current_conditions(
        route, ["Accepted", "ResolvedRefs"], parent=cfg["gateway_name"]
    ):
        raise ValueError("platform HTTPRoute conditions are not current")
    pool = kube.get("inferencepools.inference.networking.k8s.io", cfg["pool"]["name"])
    if pool["spec"]["selector"] != {"matchLabels": {"app": cfg["target"]}}:
        raise ValueError("InferencePool selector drift")
    if pool["spec"]["targetPorts"] != [{"number": cfg["pool"]["target_port"]}]:
        raise ValueError("InferencePool target port drift")


def metric_sample(kube, cfg):
    metric = cfg["metric"]
    url = (
        metric["local_query_url"].rstrip("/")
        + "/api/v1/query?"
        + urllib.parse.urlencode({"query": metric["query"]})
    )
    with urllib.request.urlopen(url, timeout=10) as response:
        payload = json.load(response)
    value = metric_value(payload, time.time(), metric["max_age_seconds"])
    result = dict(prometheus=payload, workload_total=value)
    api = (
        "/apis/custom.metrics.k8s.io/v1beta1/namespaces/"
        + kube.lab["namespace"]
        + "/deployments.apps/"
        + cfg["target"]
        + "/"
        + metric["exposed_name"]
    )
    result["adapter_api"] = kube.call(
        "get",
        "--raw",
        api
        + "?metricLabelSelector="
        + urllib.parse.quote(
            "namespace=" + kube.lab["namespace"] + ",workload=" + cfg["target"]
        ),
    )
    if len(result["adapter_api"].get("items", [])) != 1:
        raise ValueError(
            "adapter must expose exactly one selected workload-total metric"
        )
    case = cfg.get("active_case", "fixed_2")
    mode = cfg["cases"][case].get("mode", case)
    if mode == "keda":
        scaled = kube.get("scaledobject", "vllm-pressure")
        names = scaled.get("status", {}).get("externalMetricNames", [])
        if len(names) != 1:
            raise ValueError("KEDA has not published exactly one external metric")
        keda_api = (
            "/apis/external.metrics.k8s.io/v1beta1/namespaces/"
            + kube.lab["namespace"]
            + "/"
            + names[0]
            + "?labelSelector="
            + urllib.parse.quote("scaledobject.keda.sh/name=vllm-pressure")
        )
        result["keda_api"] = kube.call("get", "--raw", keda_api)
        if len(result["keda_api"].get("items", [])) != 1:
            raise ValueError("KEDA external metric is missing or ambiguous")
    return result


def apply_bundle(kube, objects, root, *, dry_run=False):
    scoped_resources(objects, kube.lab["namespace"])
    if not objects:
        return
    path = root / ("dry-run.yaml" if dry_run else "apply.yaml")
    write_text(path, yaml.safe_dump_all(objects, sort_keys=False))
    args = ["apply", "--server-side", "--field-manager=platform-study", "-f", str(path)]
    if dry_run:
        args += ["--dry-run=server", "-o", "json"]
    output = kube.call(*args, parse=False)
    write_text(root / ("admitted.json" if dry_run else "apply-result.txt"), output)


def switch_scaler(kube, cfg, mode, root):
    """Bounded handoff; never silently remove an unknown controller's HPA."""
    dep = kube.get("deployment", cfg["target"])
    if dep["metadata"].get("ownerReferences"):
        raise ValueError("scale target has a parent controller")
    available = kube.available()
    hs = kube.get("hpa")["items"]
    ss = items_if_available(kube, "scaledobjects.keda.sh", available)
    ours_s = [s for s in ss if s["spec"]["scaleTargetRef"]["name"] == cfg["target"]]
    ours_h = [h for h in hs if h["spec"]["scaleTargetRef"]["name"] == cfg["target"]]
    for obj in ours_s + ours_h:
        md = obj["metadata"]
        owned_by_keda = any(
            o.get("uid") in {s["metadata"]["uid"] for s in ours_s}
            for o in md.get("ownerReferences", [])
        )
        if (
            md.get("labels", {}).get("app.kubernetes.io/part-of") != "serving-study"
            and not owned_by_keda
        ):
            raise ValueError("unknown autoscaler on target; no handoff performed")
    write_json(
        root / "writers-before.json", dict(deployment=dep, hpas=hs, scaled_objects=ss)
    )
    for obj in ours_s:
        kube.call(
            "delete",
            "scaledobject",
            obj["metadata"]["name"],
            "--wait=false",
            parse=False,
        )
    for obj in ours_h:
        kube.call(
            "delete",
            "hpa",
            obj["metadata"]["name"],
            "--ignore-not-found",
            "--wait=false",
            parse=False,
        )
    deadline = time.monotonic() + 60
    while True:
        try:
            ownership(kube, cfg, "fixed_2")
            break
        except ValueError:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    "old autoscalers remain; new writer was not installed"
                )
            time.sleep(2)
    if mode.startswith("fixed_"):
        kube.call(
            "scale",
            "deployment/" + cfg["target"],
            "--replicas=" + mode[-1],
            parse=False,
        )
    else:
        apply_bundle(kube, scaling_resources(cfg, kube.lab, mode), root)


@contextmanager
def injected_fault(kube, cfg, versions, fault, root):
    """Restore the exact owned field in finally, including failed or cancelled traffic."""
    object_name, patch, restore = None, None, None
    if fault == "epp_unavailable":
        object_name = "deployment/" + cfg["epp_deployment"]
        obj = kube.get("deployment", cfg["epp_deployment"])
        scoped_resources([obj], kube.lab["namespace"])
        patch, restore = {"spec": {"replicas": 0}}, {
            "spec": {"replicas": obj["spec"]["replicas"]}
        }
    elif fault in {"wrong_port", "no_endpoints", "membership"}:
        object_name = "inferencepool/" + cfg["pool"]["name"]
        obj = kube.get("inferencepool", cfg["pool"]["name"])
        scoped_resources([obj], kube.lab["namespace"])
        key = "targetPorts" if fault == "wrong_port" else "selector"
        value = (
            [{"number": 65534}]
            if key == "targetPorts"
            else {"matchLabels": {"app": "no-such-lab-backend"}}
        )
        if fault == "membership":
            pods = kube.call("get", "pods", "-l", "app=" + cfg["target"], "-o", "json")[
                "items"
            ]
            if len(pods) != 2:
                raise ValueError("membership experiment requires exactly two Pods")
            selected = pods[0]
            scoped_resources([selected], kube.lab["namespace"])
            label = "serving-study-member"
            previous = selected["metadata"].get("labels", {}).get(label)
            # The temporary label selects one Pod, then is restored with the pool selector.
            try:
                kube.call(
                    "label",
                    "pod",
                    selected["metadata"]["name"],
                    label + "=selected",
                    "--overwrite",
                    parse=False,
                )
                kube.call(
                    "patch",
                    object_name,
                    "--type=json",
                    "-p",
                    json.dumps(
                        [
                            dict(
                                op="replace",
                                path="/spec/selector",
                                value={"matchLabels": {label: "selected"}},
                            )
                        ]
                    ),
                    parse=False,
                )
                snapshot(kube, cfg, root / "injected")
                yield
            finally:
                kube.call(
                    "patch",
                    object_name,
                    "--type=json",
                    "-p",
                    json.dumps(
                        [
                            dict(
                                op="replace",
                                path="/spec/selector",
                                value=obj["spec"]["selector"],
                            )
                        ]
                    ),
                    parse=False,
                )
                kube.call(
                    "label",
                    "pod",
                    selected["metadata"]["name"],
                    label + "=" + previous if previous is not None else label + "-",
                    "--overwrite",
                    parse=False,
                )
            return
        patch, restore = {"spec": {key: value}}, {"spec": {key: obj["spec"][key]}}
    elif fault == "pod_replacement":
        pods = kube.call("get", "pods", "-l", "app=" + cfg["target"], "-o", "json")[
            "items"
        ]
        if not pods:
            raise ValueError("no replacement candidate")
        scoped_resources([pods[0]], kube.lab["namespace"])
        kube.call(
            "delete", "pod", pods[0]["metadata"]["name"], "--wait=false", parse=False
        )
        yield
        return
    else:
        # Release-specific failures are explicit apply/restore artifacts, never shell snippets.
        before = documents(artifact(versions, "fault_" + fault + "_restore"))
        after = documents(artifact(versions, "fault_" + fault + "_apply"))
        scoped_resources(before, kube.lab["namespace"])
        scoped_resources(after, kube.lab["namespace"])
        try:
            apply_bundle(kube, after, root / "fault")
            yield
        finally:
            apply_bundle(kube, before, root / "restore")
        return
    try:
        kube.call(
            "patch", object_name, "--type=merge", "-p", json.dumps(patch), parse=False
        )
        if fault == "epp_unavailable":
            deadline = time.monotonic() + 60
            while (
                kube.get("deployment", cfg["epp_deployment"])
                .get("status", {})
                .get("replicas", 0)
            ):
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        "EPP Pods did not terminate before fault traffic"
                    )
                time.sleep(2)
        snapshot(kube, cfg, root / "injected")
        yield
    finally:
        kube.call(
            "patch", object_name, "--type=merge", "-p", json.dumps(restore), parse=False
        )
        write_json(
            root / "fault-restored.json",
            dict(resource=object_name, timestamp=time.time()),
        )


def sample_loop(kube, cfg, stop, errors):
    with (kube.root / "timeline.jsonl").open("x") as handle:
        while not stop.is_set():
            stamp = time.time()
            row = dict(timestamp=stamp)
            try:
                if cfg["week"] == 19:
                    row["parents"] = kube.get("llminferenceservices.serving.kserve.io")[
                        "items"
                    ]
                    row["pods"] = kube.get("pods")["items"]
                    row["allocated_gpus"] = (
                        None  # Generated workload ownership is audited separately.
                    )
                    handle.write(json.dumps(row) + "\n")
                    handle.flush()
                    stop.wait(cfg["sample_seconds"])
                    continue
                pods = kube.call(
                    "get", "pods", "-l", "app=" + cfg["target"], "-o", "json"
                )["items"]
                row["pods"] = pods
                row["allocated_gpus"] = sum(
                    int(
                        c.get("resources", {})
                        .get("requests", {})
                        .get("nvidia.com/gpu", 0)
                    )
                    for p in pods
                    if p["spec"].get("nodeName")
                    and p.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
                    for c in p["spec"]["containers"]
                )
                row["deployment"] = kube.get("deployment", cfg["target"])
                row["hpas"] = kube.get("hpa")["items"]
                if cfg["week"] == 20:
                    try:
                        row["metric"] = metric_sample(kube, cfg)
                    except Exception as error:
                        row["metric_error"] = type(error).__name__
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                row["collection_error"] = type(error).__name__
                errors.append(row["collection_error"])
            handle.write(json.dumps(row) + "\n")
            handle.flush()
            stop.wait(cfg["sample_seconds"])


def warmup(kube, cfg, base, tokenizer, repeat, cache, root):
    if cfg["week"] == 19:
        return  # Composition/CPU stub audits do not masquerade as cache experiments.
    # Restarting is explicit in --run; preserve writer ownership and the exact Pod template.
    kube.call("rollout", "restart", "deployment/" + cfg["target"], parse=False)
    deadline = time.monotonic() + cfg["ready_timeout_seconds"]
    while True:
        try:
            ready(kube, cfg, base, cfg["active_case"])
            break
        except ValueError:
            if time.monotonic() > deadline:
                raise TimeoutError("cache-reset rollout did not become Ready")
            time.sleep(2)
    jobs = make_jobs(cfg, tokenizer, "short", 1, repeat, warmup=True)
    if cache == "warm":
        # Build actual shared-family tokens, independent of the warmup-only leading ID.
        primer_cfg = {**cfg, "requests": cfg["prefix_families"]}
        primers = make_jobs(primer_cfg, tokenizer, "shared_prefix", 1, repeat)
        jobs += [
            {
                **j,
                "prompt_ids": j["prompt_ids"][: cfg["shared_tokens"]],
                "output_tokens": 1,
            }
            for j in primers
        ]
    pods = kube.call("get", "pods", "-l", "app=" + cfg["target"], "-o", "json")["items"]
    code = (
        "import json,sys,urllib.request; p=json.loads(sys.argv[1]); "
        "r=urllib.request.Request('http://127.0.0.1:8000/v1/completions', "
        "data=json.dumps(p).encode(),headers={'Content-Type':'application/json'}); "
        "v=json.load(urllib.request.urlopen(r,timeout=120)); print(json.dumps({'usage':v.get('usage')}))"
    )
    evidence = []
    for pod in pods:
        if pod["metadata"].get("deletionTimestamp"):
            continue
        for j in jobs:
            payload = dict(
                model=base["model"]["served_model_name"],
                prompt=j["prompt_ids"],
                max_tokens=j["output_tokens"],
                ignore_eos=True,
                temperature=0,
                stream=False,
            )
            # This command includes synthetic IDs: deliberately bypass the command journal.
            argv = [
                "kubectl",
                "--context",
                kube.lab["context"],
                "-n",
                kube.lab["namespace"],
                "--request-timeout=20s",
                "exec",
                pod["metadata"]["name"],
                "-c",
                "identity",
                "--",
                "python",
                "-c",
                code,
                json.dumps(payload),
            ]
            answer = json.loads(
                subprocess.check_output(
                    argv, text=True, stderr=subprocess.DEVNULL, timeout=140
                )
            )
            if (answer.get("usage") or {}).get("completion_tokens") != j[
                "output_tokens"
            ]:
                raise ValueError("per-Pod model/prefix warmup failed")
            evidence.append(
                dict(
                    pod_uid=pod["metadata"]["uid"],
                    output_tokens=j["output_tokens"],
                    timestamp=time.time(),
                )
            )
    write_json(root / "warmup.json", dict(cache=cache, samples=evidence))


def run_cell(kube, cfg, base, versions, args, root):
    from src.study_runner import load_tokenizer

    cfg = {**cfg, "active_case": args.case}
    ready(kube, cfg, base, args.case)
    verify_live_case(kube, cfg, base, versions, args.case)
    if cfg["week"] == 20:
        write_json(root / "metric-preflight.json", metric_sample(kube, cfg))
    tokenizer = load_tokenizer(base)
    rate = cfg["rates"][args.rate] or base.get("near_slo_rps")
    if rate is None:
        raise ValueError("freeze measured near-SLO rate first")
    jobs = make_jobs(cfg, tokenizer, args.workload, rate, args.repeat)
    write_json(root / "trace-metadata.json", trace_metadata(jobs))
    jobs = [{**j, "request_id": root.name + "-" + j["request_id"]} for j in jobs]
    stop, errors = threading.Event(), []
    sampler = threading.Thread(
        target=sample_loop, args=(kube, cfg, stop, errors), daemon=True
    )
    sampler.start()
    try:
        warmup(kube, cfg, base, tokenizer, args.repeat, args.cache, root)
        write_json(root / "measurement-start.json", dict(timestamp=time.time()))

        def traffic():
            return run_requests(
                cfg, base, jobs, root / "clients.jsonl", cancel=args.cancel_smoke
            )

        fault = cfg["cases"][args.case].get("fault") if args.action == "fault" else None
        if fault:
            with injected_fault(kube, cfg, versions, fault, root):
                rows, summary = traffic()
        else:
            rows, summary = traffic()
        lags = [
            r["arrival_lag_ms"] for r in rows if r.get("arrival_lag_ms") is not None
        ]
        client_saturated = any(r["status"] == "client_overload" for r in rows) or any(
            lag > base["max_arrival_lag_ms"] for lag in lags
        )
        summary.update(
            cancel_smoke=args.cancel_smoke,
            fault=fault,
            client_saturated=client_saturated,
            performance_eligible=cfg["week"] != 19
            and not fault
            and not args.cancel_smoke
            and not client_saturated,
            attribution_status="requires_external_gateway_epp_and_engine_evidence",
        )
        write_json(root / "summary.json", summary)
        if fault:
            deadline = time.monotonic() + cfg["ready_timeout_seconds"]
            while True:
                try:
                    ready(kube, cfg, base, args.case)
                    verify_live_case(kube, cfg, base, versions, args.case)
                    break
                except ValueError:
                    if time.monotonic() > deadline:
                        raise TimeoutError("fault recovery did not become Ready")
                    time.sleep(2)
            smoke = [{**jobs[0], "offset": 0, "request_id": root.name + "-recovery"}]
            recovery, _ = run_requests(cfg, base, smoke, root / "recovery.jsonl")
            if recovery[0]["status"] != "success":
                raise ValueError("post-fault generation smoke failed")
    finally:
        stop.set()
        sampler.join(timeout=150)
        write_json(
            root / "timeline-status.json",
            dict(
                ended_at=time.time(),
                collection_errors=errors,
                sampler_stopped=not sampler.is_alive(),
            ),
        )


def kserve_action(kube, cfg, versions, args, root):
    name, kind = cfg["llmisvc_name"], "llminferenceservice.serving.kserve.io"
    parent = kube.get(kind, name)
    scoped_resources([parent], kube.lab["namespace"])
    if args.action == "delete":
        write_json(root / "deleted-parent.json", parent)
        # Only this parent is deleted. Shared config/Gateway/CRDs are audited, never deleted here.
        kube.call("delete", kind, name, "--wait=false", parse=False)
    else:
        changed = documents(artifact(versions, "kserve_mutation"))
        if (
            len(changed) != 1
            or changed[0]["kind"] != "LLMInferenceService"
            or changed[0]["metadata"]["name"] != name
        ):
            raise ValueError(
                "mutation must update only the declared LLMInferenceService"
            )
        apply_bundle(kube, changed, root)
    deadline = time.monotonic() + cfg["ready_timeout_seconds"]
    while True:
        parents = kube.get("llminferenceservices.serving.kserve.io")["items"]
        found = next((p for p in parents if p["metadata"]["name"] == name), None)
        if args.action == "delete" and found is None:
            break
        if (
            args.action == "mutate"
            and found
            and found["metadata"]["generation"] > parent["metadata"]["generation"]
            and current_conditions(found, ["Ready"])
        ):
            break
        if time.monotonic() > deadline:
            raise TimeoutError(
                "LLMInferenceService action did not converge; inspect snapshots/events"
            )
        time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    actions = parser.add_mutually_exclusive_group(required=True)
    for action in (
        "plan",
        "render",
        "preflight",
        "server-dry-run",
        "apply",
        "snapshot",
        "run",
        "fault",
        "mutate",
        "delete",
        "restore",
        "conformance",
    ):
        actions.add_argument(
            "--" + action, dest="action", action="store_const", const=action
        )
    parser.add_argument("--case")
    parser.add_argument("--workload")
    parser.add_argument("--rate", default="low")
    parser.add_argument("--cache", choices=["cold", "warm"], default="cold")
    parser.add_argument("--repeat", type=int, default=0)
    parser.add_argument("--cancel-smoke", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    cfg, base, lab, versions = load_study(args.config)
    args.case = args.case or next(iter(cfg["cases"]))
    args.workload = args.workload or cfg["workloads"][0]
    if (
        args.case not in cfg["cases"]
        or args.workload not in cfg["workloads"]
        or args.rate not in cfg["rates"]
    ):
        parser.error("unknown case/workload/rate")
    if not 0 <= args.repeat < cfg["repeats"]:
        parser.error("repeat outside configured independent repetitions")
    if args.action == "plan":
        print(
            json.dumps(
                dict(
                    week=cfg["week"],
                    cells=matrix(cfg),
                    faults=[n for n, c in cfg["cases"].items() if c.get("fault")],
                    required_artifacts=cfg["required_artifacts"],
                    runtime_executed=False,
                ),
                indent=2,
            )
        )
        return
    if args.action == "render":
        bundle = render(cfg, base, lab, versions, args.case, template=True)
        output = (
            "# Template only; release-specific artifacts and live admission are required.\n"
            + yaml.safe_dump_all(bundle, sort_keys=False)
        )
        if args.output:
            write_text(args.output, output)
        else:
            print(output)
        return
    if args.action in {"mutate", "delete"} and cfg["week"] != 19:
        parser.error("mutate/delete apply only to the Week 19 parent")
    if args.action == "conformance" and cfg["week"] != 17:
        parser.error("conformance applies only to Week 17")
    if args.action == "restore" and cfg["week"] != 20:
        parser.error("restore selects Week 20 fixed_2")
    if args.action == "fault" and not cfg["cases"][args.case].get("fault"):
        parser.error("--fault requires a fault case")
    if args.action == "run" and cfg["cases"][args.case].get("fault"):
        parser.error("fault cases require --fault")
    root = Path(args.output or cfg["output_root"]) / (
        time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:10]
    )
    root.mkdir(parents=True, exist_ok=False)
    state = dict(
        status="running",
        started_at=time.time(),
        args=vars(args),
        config=cfg,
        baseline=base,
        lab=lab,
        versions=versions,
        source=source_identity(),
    )
    write_json(root / "run.json", state)
    kube = None
    try:
        validate_versions(cfg, base, lab, versions)
        for key in cfg["required_artifacts"]:
            data = artifact(versions, key)
            write_text(root / "artifacts" / (key + ".txt"), data.decode())
        kube = Cluster(lab, root)
        schema_preflight(kube, cfg, versions, root)
        snapshot(kube, cfg, root / "before")
        if args.action in {"apply", "server-dry-run"}:
            if cfg["week"] == 20 and args.action == "apply":
                if not versions.get("replicas_sync_disabled"):
                    raise ValueError(
                        "disable GitOps/manual replicas synchronization before handoff"
                    )
                switch_scaler(
                    kube, cfg, cfg["cases"][args.case].get("mode", args.case), root
                )
            elif cfg["week"] == 20 and args.case.startswith("fixed_"):
                output = kube.call(
                    "scale",
                    "deployment/" + cfg["target"],
                    "--replicas=" + args.case[-1],
                    "--dry-run=server",
                    "-o",
                    "json",
                    parse=False,
                )
                write_text(root / "admitted.json", output)
            else:
                if cfg["week"] in {17, 18}:
                    # Existing targets must have no autoscaling writer before reapplying replicas.
                    deployments = kube.get("deployment")["items"]
                    if any(d["metadata"]["name"] == cfg["target"] for d in deployments):
                        ownership(kube, cfg, "fixed_2")
                apply_bundle(
                    kube,
                    render(cfg, base, lab, versions, args.case),
                    root,
                    dry_run=args.action == "server-dry-run",
                )
        elif args.action in {"run", "fault"}:
            run_cell(kube, cfg, base, versions, args, root)
        elif args.action == "restore":
            switch_scaler(kube, cfg, "fixed_2", root)
        elif args.action in {"mutate", "delete"}:
            kserve_action(kube, cfg, versions, args, root)
        elif args.action == "conformance":
            # A reviewed, pinned official suite is executed as a namespaced Job.
            jobs = documents(artifact(versions, "conformance_job"))
            if len(jobs) != 1 or jobs[0]["kind"] != "Job":
                raise ValueError("conformance artifact must be one bounded Job")
            images = manifest_images(jobs)
            if not images or not all(digest(image) for image in images):
                raise ValueError("pin conformance Job images by digest")
            spec = jobs[0]["spec"]
            if (
                not 1 <= spec.get("activeDeadlineSeconds", 0) <= 1800
                or spec.get("backoffLimit") != 0
            ):
                raise ValueError(
                    "conformance Job needs <=1800s deadline and no retries"
                )
            apply_bundle(kube, jobs, root)
            name = jobs[0]["metadata"]["name"]
            deadline = time.monotonic() + spec["activeDeadlineSeconds"] + 30
            while True:
                job = kube.get("job", name)
                write_json(root / "conformance-status.json", job)
                if job.get("status", {}).get("succeeded"):
                    break
                if job.get("status", {}).get("failed") or time.monotonic() > deadline:
                    raise RuntimeError("official conformance Job failed or timed out")
                time.sleep(2)
            write_text(
                root / "conformance.txt", kube.call("logs", "job/" + name, parse=False)
            )
        state["status"] = "completed"
    except BaseException as error:
        state.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        if kube:
            try:
                snapshot(kube, cfg, root / "after")
            except Exception as error:
                state["final_snapshot_error"] = type(error).__name__
        state["ended_at"] = time.time()
        write_json(root / "run.json", state)
        print(str(root))


if __name__ == "__main__":
    main()
