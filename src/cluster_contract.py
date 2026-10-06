"""Kubernetes manifests and evidence checks for complete single-node replicas."""

from __future__ import annotations

import copy
import math
import re

from src.serving_experiment import engine_command, validate_baseline


def digest(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", value))


def validate_lock(lock, base, week):
    validate_baseline(base, frozen=True)
    if not lock.get("context") or not lock.get("independent_lab"):
        raise ValueError("an explicit independent lab context is required")
    if lock["namespace"] in {"default", "kube-system", "kube-public"}:
        raise ValueError("use a dedicated experiment namespace")
    if not lock.get("private_entrypoint_verified"):
        raise ValueError("verify the controller's private/internal entrypoint policy first")
    if not digest(base.get("image")) or not digest(lock.get("gateway_image")):
        raise ValueError("pin vLLM and lab proxy images by digest")
    if lock["max_gpu_slots"] < 2 * base["gpus_per_replica"]:
        raise ValueError("lab needs two complete replica slots")
    if week == 16:
        api = lock["gateway_api"]
        if not all(api.values()) or not digest(api["controller_image"]) or not digest(api["dataplane_image"]):
            raise ValueError("freeze CRD/controller/data-plane versions and capability evidence")


def meta(name, namespace=None, labels=None):
    return dict(name=name, **({"namespace": namespace} if namespace else {}),
                labels={"app.kubernetes.io/part-of": "serving-study", **(labels or {})})


def service(name, selector, ns, *, headless=False, port=8081):
    return dict(apiVersion="v1", kind="Service", metadata=meta(name, ns),
                spec=dict(selector=selector, type="ClusterIP", ports=[dict(name="http", port=80, targetPort=port)],
                          **({"clusterIP": "None"} if headless else {})))


def deployment(name, replicas, base, lock):
    ns = lock["namespace"]
    labels = {"app": name, "serving-study-role": "engine"}
    gpus = base["gpus_per_replica"]
    shape = dict(model=base["model"], engine=base["engine"])
    argv = engine_command(shape, host="127.0.0.1")
    def field(key):
        return dict(valueFrom=dict(fieldRef=dict(fieldPath=key)))

    identity = dict(name="identity", image=lock["gateway_image"],
        ports=[dict(name="http", containerPort=8081)],
        env=[dict(name="LOCAL_ENGINE", value="1"), dict(name="LISTEN_HOST", value="0.0.0.0"),
             dict(name="PORT", value="8081"), dict(name="POD_UID", **field("metadata.uid")),
             dict(name="POD_NAME", **field("metadata.name"))],
        resources=dict(requests=dict(cpu="100m", memory="128Mi"), limits=dict(cpu="1", memory="256Mi")),
        readinessProbe=dict(httpGet=dict(path="/health", port="http"), periodSeconds=2))
    engine = dict(name="vllm", image=base["image"], command=["vllm"], args=argv[1:],
        env=[dict(name="VLLM_USE_V1", value="1")],
        resources=dict(requests={"cpu": base["cpu"], "memory": base["memory"], "nvidia.com/gpu": gpus},
                       limits={"cpu": base["cpu"], "memory": base["memory"], "nvidia.com/gpu": gpus}),
        startupProbe=dict(exec=dict(command=["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]),
                          failureThreshold=180, periodSeconds=5),
        readinessProbe=dict(exec=dict(command=["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]), periodSeconds=2),
        lifecycle=dict(preStop=dict(exec=dict(command=["python", "-c", "import time; time.sleep(10)"]))),
        volumeMounts=[dict(name="shm", mountPath="/dev/shm"), dict(name="model-cache", mountPath="/root/.cache/huggingface")])
    return dict(apiVersion="apps/v1", kind="Deployment", metadata=meta(name, ns),
        spec=dict(replicas=replicas, revisionHistoryLimit=2,
                  strategy=dict(type="RollingUpdate", rollingUpdate=dict(maxSurge=0, maxUnavailable=1)),
                  selector=dict(matchLabels={"app": name}),
                  template=dict(metadata=dict(labels={**labels, "app.kubernetes.io/part-of": "serving-study"}),
                    spec=dict(automountServiceAccountToken=False, terminationGracePeriodSeconds=160,
                        nodeSelector={base["gpu_node_selector"]["key"]: base["gpu_node_selector"]["value"]},
                        containers=[engine, identity], volumes=[dict(name="shm", emptyDir=dict(medium="Memory", sizeLimit="2Gi")),
                            dict(name="model-cache", emptyDir=dict(sizeLimit="8Gi"))]))))


def rr_resources(base, lock, *, replicas=2, hpa=False):
    ns = lock["namespace"]
    name = "vllm"
    resources = [deployment(name, replicas, base, lock), service("vllm-pool", {"app": name}, ns, headless=True)]
    resources += [dict(apiVersion="v1", kind="ServiceAccount", metadata=meta("rr-discovery", ns)),
        dict(apiVersion="rbac.authorization.k8s.io/v1", kind="Role", metadata=meta("rr-discovery", ns),
             rules=[dict(apiGroups=["discovery.k8s.io"], resources=["endpointslices"], verbs=["get", "list"])]),
        dict(apiVersion="rbac.authorization.k8s.io/v1", kind="RoleBinding", metadata=meta("rr-discovery", ns),
             roleRef=dict(apiGroup="rbac.authorization.k8s.io", kind="Role", name="rr-discovery"),
             subjects=[dict(kind="ServiceAccount", name="rr-discovery", namespace=ns)])]
    resources.append(dict(apiVersion="apps/v1", kind="Deployment", metadata=meta("rr-gateway", ns),
        spec=dict(replicas=1, selector=dict(matchLabels={"app": "rr-gateway"}),
            template=dict(metadata=dict(labels={"app": "rr-gateway", "app.kubernetes.io/part-of": "serving-study"}),
                spec=dict(serviceAccountName="rr-discovery", terminationGracePeriodSeconds=150,
                    containers=[dict(name="gateway", image=lock["gateway_image"], ports=[dict(name="http", containerPort=8080)],
                        env=[dict(name="DISCOVERY_SERVICE", value="vllm-pool"), dict(name="LISTEN_HOST", value="0.0.0.0"),
                             dict(name="POD_NAMESPACE", value=ns), dict(name="POD_UID", valueFrom=dict(fieldRef=dict(fieldPath="metadata.uid")))],
                        resources=dict(requests=dict(cpu="500m", memory="128Mi"), limits=dict(cpu="2", memory="512Mi")),
                        readinessProbe=dict(httpGet=dict(path="/health", port="http"), periodSeconds=2))])))))
    resources.append(service("rr-gateway", {"app": "rr-gateway"}, ns, port=8080))
    if hpa:
        resources.append(hpa_resource(ns))
    return resources


def hpa_resource(ns):
    return dict(apiVersion="autoscaling/v2", kind="HorizontalPodAutoscaler", metadata=meta("vllm-cpu", ns),
        spec=dict(scaleTargetRef=dict(apiVersion="apps/v1", kind="Deployment", name="vllm"), minReplicas=1, maxReplicas=2,
            metrics=[dict(type="Resource", resource=dict(name="cpu", target=dict(type="Utilization", averageUtilization=70)))],
            behavior=dict(scaleDown=dict(stabilizationWindowSeconds=120), scaleUp=dict(stabilizationWindowSeconds=0))))


def gateway_resources(base, lock):
    ns, api = lock["namespace"], lock["gateway_api"]
    resources = []
    for suffix in ("a", "b"):
        name = "vllm-" + suffix
        resources += [deployment(name, 1, base, lock), service(name, {"app": name}, ns)]
    resources.append(dict(apiVersion="gateway.networking.k8s.io/v1", kind="GatewayClass",
        metadata=meta(api["class_name"]), spec=dict(controllerName=api["controller_name"])))
    listener = dict(name="http", protocol="HTTP", port=80, hostname="inference.lab.invalid",
                    allowedRoutes={"namespaces": {"from": "Same"}})
    resources.append(dict(apiVersion="gateway.networking.k8s.io/v1", kind="Gateway", metadata=meta("inference", ns),
                          spec=dict(gatewayClassName=api["class_name"], listeners=[listener])))
    rules = []
    for label, weights in (("only-a", (100, 0)), ("half", (50, 50)), ("canary", (90, 10)),
                           ("match-a", (100, 0)), ("match-b", (0, 100))):
        rules.append(dict(matches=[dict(path=dict(type="Exact", value="/v1/completions"),
            headers=[dict(type="Exact", name="X-Lab-Case", value=label)])],
            backendRefs=[dict(name="vllm-" + suffix, port=80, weight=weight) for suffix, weight in zip(("a", "b"), weights)]))
    route = dict(apiVersion="gateway.networking.k8s.io/v1", kind="HTTPRoute", metadata=meta("inference", ns),
                 spec=dict(parentRefs=[dict(name="inference", sectionName="http")], hostnames=["inference.lab.invalid"], rules=rules))
    resources.append(route)
    return resources


def negative_route(lock, case):
    if case not in {"invalid_backend", "wrong_port"}:
        raise ValueError("unknown negative route")
    return dict(apiVersion="gateway.networking.k8s.io/v1", kind="HTTPRoute",
        metadata=meta(case.replace("_", "-"), lock["namespace"]),
        spec=dict(parentRefs=[dict(name="inference", sectionName="http")], hostnames=["inference.lab.invalid"],
            rules=[dict(matches=[dict(path=dict(type="Exact", value="/v1/completions"),
                headers=[dict(type="Exact", name="X-Lab-Case", value="invalid" if case == "invalid_backend" else "wrong-port")])],
                backendRefs=[dict(name="missing-backend" if case == "invalid_backend" else "vllm-a", port=81 if case == "wrong_port" else 80)])]))


def current_conditions(resource, required, *, parent=None):
    generation = resource["metadata"]["generation"]
    if parent is None:
        conditions = resource.get("status", {}).get("conditions", [])
    else:
        matches = [p for p in resource.get("status", {}).get("parents", [])
                   if p.get("parentRef", {}).get("name") == parent
                   and p.get("parentRef", {}).get("namespace", resource["metadata"].get("namespace")) == resource["metadata"].get("namespace")]
        if len(matches) != 1:
            return False
        conditions = matches[0].get("conditions", [])
    return all(any(c["type"] == kind and c["status"] == "True" and c.get("observedGeneration") == generation
                   for c in conditions) for kind in required)


def verify_rank_evidence(pod, evidence, gpus):
    """The caller must supply actual worker/rank evidence, not derive it from YAML."""
    if evidence.get("pod_uid") != pod["metadata"]["uid"] or evidence.get("node") != pod["spec"]["nodeName"]:
        raise ValueError("Pod/node identity mismatch")
    ranks = evidence["ranks"]
    if evidence["tensor_parallel_size"] != gpus or len(ranks) != gpus:
        raise ValueError("rank/TP count mismatch")
    if {r["local_rank"] for r in ranks} != set(range(gpus)) or len({r["gpu_uuid"] for r in ranks}) != gpus:
        raise ValueError("ranks must map to distinct local GPUs")
    if any(r["node"] != evidence["node"] for r in ranks):
        raise ValueError("replica spans nodes")
    container = next(c for c in pod["spec"]["containers"] if c["name"] == "vllm")
    if int(container["resources"]["limits"]["nvidia.com/gpu"]) != gpus:
        raise ValueError("GPU allocation mismatch")
    return True


def weight_verdict(a, b, weights, *, minimum=1000, z=3.290526731):
    """99.9% Wilson interval fixed before measurement; small samples stay inconclusive."""
    n = a + b
    if min(a, b) < 0 or sum(weights) <= 0:
        raise ValueError("invalid counts/weights")
    expected = weights[0] / sum(weights)
    if n < minimum:
        return dict(status="insufficient_samples", samples=n, expected=expected)
    p, z2 = a / n, z * z
    center = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    compatible = (b == 0 if expected == 1 else a == 0 if expected == 0 else center - half <= expected <= center + half)
    return dict(status="compatible" if compatible else "outside_interval", samples=n,
                observed=p, expected=expected, lower=center - half, upper=center + half)


def template_bundle(base, lock, week):
    base, lock = copy.deepcopy(base), copy.deepcopy(lock)
    base["image"] = base["image"] or "REPLACE_WITH_VLLM_IMAGE_DIGEST"
    lock["gateway_image"] = lock["gateway_image"] or "REPLACE_WITH_PROXY_IMAGE_DIGEST"
    return rr_resources(base, lock) if week == 15 else gateway_resources(base, lock)
