"""Version, resource and unique-writer contracts for Weeks 17-20 (CPU safe)."""

from __future__ import annotations

import copy
import hashlib
import math
from pathlib import Path
import re

import yaml

from src.cluster_contract import deployment, digest, meta, service, validate_lock
from src.common import load_yaml
from src.serving_experiment import validate_baseline


def load_study(path):
    cfg = load_yaml(path)
    if cfg.get("schema_version") != 1 or cfg.get("week") not in range(17, 21):
        raise ValueError("expected Week 17-20 schema_version=1")
    base = validate_baseline(load_yaml(cfg["baseline"]))
    for key in (
        "requests",
        "repeats",
        "max_inflight",
        "warmup_requests",
        "prefix_families",
    ):
        if type(cfg[key]) is not int or cfg[key] < 1:
            raise ValueError(f"invalid {key}")
    if cfg["requests"] > 10000 or cfg["max_inflight"] > 512 or cfg["repeats"] < 3:
        raise ValueError("require >=3 repeats, <=10000 requests and <=512 inflight")
    for key in ("timeout_seconds", "max_run_seconds", "sample_seconds"):
        if not math.isfinite(cfg[key]) or cfg[key] <= 0:
            raise ValueError(f"invalid {key}")
    if cfg["max_run_seconds"] > 3600:
        raise ValueError("one cell must fit in one hour")
    for shape in cfg["shapes"].values():
        if any(type(v) is not int or v <= 0 for v in shape.values()):
            raise ValueError("invalid token shape")
        if sum(shape.values()) > base["engine"]["max_model_len"]:
            raise ValueError("workload exceeds model context")
    if not 0 < cfg["shared_tokens"] < cfg["shapes"]["shared"]["prompt_tokens"]:
        raise ValueError("prefix must leave a unique suffix")
    return cfg, base, load_yaml(cfg["cluster_lock"]), load_yaml(cfg["versions_lock"])


def artifact(lock, key):
    record = lock.get("artifacts", {}).get(key) or {}
    path = Path(record.get("path") or "__missing_artifact__")
    if not path.is_file():
        raise ValueError(f"supply fixed-release artifact: {key}")
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if record.get("sha256") != actual:
        raise ValueError(f"artifact checksum mismatch: {key}")
    return data


def documents(data):
    items = []
    for obj in yaml.safe_load_all(data):
        if not obj:
            continue
        items.extend(obj["items"] if obj.get("kind") == "List" else [obj])
    return items


def manifest_images(value):
    images = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"containers", "initContainers"} and isinstance(child, list):
                images += [
                    c["image"] for c in child if isinstance(c, dict) and "image" in c
                ]
            else:
                images += manifest_images(child)
    elif isinstance(value, list):
        for child in value:
            images += manifest_images(child)
    return images


def validate_versions(cfg, base, lab, lock):
    validate_lock(lab, base, 16)
    if not lock.get("frozen") or lock.get("gaie_release") != "v1.0.0":
        raise ValueError("freeze the compatibility lock with GAIE v1.0.0")
    required = ["reference_epp"] if cfg["week"] == 17 else ["llmd"]
    if cfg["week"] == 19:
        required += ["kserve"]
    if cfg["week"] == 20:
        required += ["adapter", "keda"]
    for name in required:
        component = lock.get("components", {}).get(name, {})
        if (
            not component.get("release")
            or component["release"] in {"main", "latest"}
            or not re.fullmatch(r"[a-f0-9]{40}", component.get("source_commit") or "")
            or not component.get("images")
            or not all(map(digest, component["images"]))
        ):
            raise ValueError(f"pin release/source commit/image digests: {name}")
    for key in cfg["required_artifacts"]:
        artifact(lock, key)
    image_keys = (
        ["reference_epp", "gateway_glue"]
        if cfg["week"] == 17
        else (
            ["llmd_load", "llmd_prefix", "gateway_glue"]
            if cfg["week"] == 18
            else (
                ["kserve_managed", "kserve_referenced"]
                if cfg["week"] == 19
                else ["llmd_load"]
            )
        )
    )
    allowed_images = {
        base["image"],
        lab["gateway_image"],
        lab["gateway_api"]["controller_image"],
        lab["gateway_api"]["dataplane_image"],
    }
    for component in lock["components"].values():
        allowed_images.update(component.get("images", []))
    for key in image_keys:
        for image in manifest_images(documents(artifact(lock, key))):
            if not digest(image) or image not in allowed_images:
                raise ValueError(f"artifact image must match a declared digest: {key}")
    if cfg["week"] == 19 and not re.fullmatch(
        r"serving\.kserve\.io/v1alpha[0-9]+", lock.get("kserve_api_version") or ""
    ):
        raise ValueError("record the actual served KServe alpha API group/version")
    if cfg["week"] in (18, 20) and not base["engine"]["enable_prefix_caching"]:
        raise ValueError("freeze the APC-on baseline for routing/scaling studies")
    if cfg["week"] == 20 and not cfg["metric"].get("calibration_evidence"):
        raise ValueError("calibrate and freeze the inference-pressure threshold first")


def crd_schema(crds, kind, version):
    matches = [
        c
        for c in crds
        if c.get("kind") == "CustomResourceDefinition"
        and c["spec"]["names"]["kind"] == kind
    ]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {kind} CRD")
    versions = [
        v
        for v in matches[0]["spec"]["versions"]
        if v["name"] == version and v.get("served")
    ]
    if len(versions) != 1:
        raise ValueError(f"{kind}/{version} is not served by the pinned CRD")
    return versions[0]["schema"]["openAPIV3Schema"]


def check_pool_schema(crds):
    schema = crd_schema(crds, "InferencePool", "v1")
    spec = schema["properties"]["spec"]["properties"]
    failure = spec["endpointPickerRef"]["properties"]["failureMode"]
    if failure.get("default") != "FailClose" or set(failure.get("enum", [])) != {
        "FailOpen",
        "FailClose",
    }:
        raise ValueError("pinned InferencePool failureMode contract differs")
    for key in ("selector", "targetPorts"):
        if key not in spec:
            raise ValueError(f"missing InferencePool field: {key}")
    return schema


def pool_resource(cfg, lab, failure_mode="FailClose"):
    pool = cfg["pool"]
    picker = dict(name=pool["epp_service"], port=dict(number=pool["epp_port"]))
    if failure_mode is not None:
        if failure_mode not in {"FailOpen", "FailClose"}:
            raise ValueError("unknown failure mode")
        picker["failureMode"] = failure_mode
    return dict(
        apiVersion="inference.networking.k8s.io/v1",
        kind="InferencePool",
        metadata=meta(pool["name"], lab["namespace"]),
        spec=dict(
            selector=dict(matchLabels={"app": cfg["target"]}),
            targetPorts=[dict(number=pool["target_port"])],
            endpointPickerRef=picker,
        ),
    )


def data_plane(cfg, base, lab, case):
    """A complete replica is selected; the identity port and metrics port are distinct."""
    base, lab = copy.deepcopy(base), copy.deepcopy(lab)
    base["image"] = base["image"] or "REPLACE_WITH_VLLM_IMAGE_DIGEST"
    lab["gateway_image"] = lab["gateway_image"] or "REPLACE_WITH_PROXY_IMAGE_DIGEST"
    ns, target = lab["namespace"], cfg["target"]
    dep = deployment(target, 2, base, lab)
    engine = dep["spec"]["template"]["spec"]["containers"][0]
    engine["args"][engine["args"].index("--host") + 1] = "0.0.0.0"
    engine["ports"] = [dict(name="engine-http", containerPort=8000)]
    pool = pool_resource(cfg, lab, case.get("failure_mode", "FailClose"))
    backend = dict(
        group="inference.networking.k8s.io",
        kind="InferencePool",
        name=cfg["pool"]["name"],
        port=cfg["pool"]["target_port"],
    )
    if case.get("baseline"):
        backend = dict(name="vllm-direct", port=80)
    route = dict(
        apiVersion="gateway.networking.k8s.io/v1",
        kind="HTTPRoute",
        metadata=meta("inference-platform", ns),
        spec=dict(
            parentRefs=[dict(name=cfg["gateway_name"], sectionName="http")],
            hostnames=[cfg["hostname"]],
            rules=[
                dict(
                    matches=[dict(path=dict(type="Exact", value="/v1/completions"))],
                    backendRefs=[backend],
                )
            ],
        ),
    )
    metrics = service("vllm-metrics", {"app": target}, ns, port=8000)
    metrics["spec"]["ports"][0].update(name="metrics", port=8000)
    return [dep, service("vllm-direct", {"app": target}, ns), metrics, pool, route]


def scoped_resources(items, namespace):
    """Only explicitly labelled namespaced lab resources may be applied by this runner."""
    forbidden = {
        "CustomResourceDefinition",
        "Namespace",
        "ClusterRole",
        "ClusterRoleBinding",
        "GatewayClass",
        "APIService",
        "Secret",
        "ValidatingWebhookConfiguration",
        "MutatingWebhookConfiguration",
    }
    seen = set()
    for obj in items:
        md = obj.get("metadata", {})
        if (
            obj.get("kind") in forbidden
            or md.get("namespace") != namespace
            or md.get("labels", {}).get("app.kubernetes.io/part-of") != "serving-study"
        ):
            raise ValueError(
                "apply requires labelled namespaced lab objects; review installations separately"
            )
        key = (obj["apiVersion"], obj["kind"], md["name"])
        if key in seen:
            raise ValueError(f"duplicate rendered resource: {key}")
        seen.add(key)
    return items


def require_subset(expected, actual, path="object"):
    """Allow server defaults, reject a run labelled with a different live treatment."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise ValueError(f"live treatment mismatch: {path}")
        for key, value in expected.items():
            if key not in actual:
                raise ValueError(f"live treatment missing: {path}.{key}")
            require_subset(value, actual[key], f"{path}.{key}")
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            raise ValueError(f"live treatment list mismatch: {path}")
        for i, (left, right) in enumerate(zip(expected, actual)):
            require_subset(left, right, f"{path}[{i}]")
    elif expected != actual:
        raise ValueError(f"live treatment value mismatch: {path}")


def scaling_resources(cfg, lab, mode):
    """Same total/replica denominator; custom Object HPA avoids KEDA APIService collision."""
    ns, target, metric = lab["namespace"], cfg["target"], cfg["metric"]
    behavior = dict(
        scaleUp=dict(stabilizationWindowSeconds=0),
        scaleDown=dict(stabilizationWindowSeconds=cfg["cooldown_seconds"]),
    )
    ref = dict(apiVersion="apps/v1", kind="Deployment", name=target)
    if mode == "hpa":
        object_metric = dict(
            describedObject=ref,
            metric=dict(
                name=metric["exposed_name"],
                selector=dict(matchLabels=dict(namespace=ns, workload=target)),
            ),
            target=dict(type="AverageValue", averageValue=str(metric["threshold"])),
        )
        return [
            dict(
                apiVersion="autoscaling/v2",
                kind="HorizontalPodAutoscaler",
                metadata=meta("vllm-pressure", ns),
                spec=dict(
                    scaleTargetRef=ref,
                    minReplicas=1,
                    maxReplicas=2,
                    behavior=behavior,
                    metrics=[dict(type="Object", object=object_metric)],
                ),
            )
        ]
    if mode == "keda":
        return [
            dict(
                apiVersion="keda.sh/v1alpha1",
                kind="ScaledObject",
                metadata=meta("vllm-pressure", ns),
                spec=dict(
                    scaleTargetRef=ref,
                    minReplicaCount=1,
                    maxReplicaCount=2,
                    pollingInterval=cfg["polling_seconds"],
                    cooldownPeriod=cfg["cooldown_seconds"],
                    advanced=dict(
                        horizontalPodAutoscalerConfig=dict(behavior=behavior)
                    ),
                    triggers=[
                        dict(
                            type="prometheus",
                            metricType="AverageValue",
                            metadata=dict(
                                serverAddress=metric["prometheus_url"],
                                query=metric["query"],
                                threshold=str(metric["threshold"]),
                                ignoreNullValues="false",
                            ),
                        )
                    ],
                ),
            )
        ]
    if mode not in {"fixed_1", "fixed_2"}:
        raise ValueError(
            "unsupported scaling mode (zero is outside the default SLO matrix)"
        )
    return []


def writer_check(deployment_obj, hpas, scaled_objects, mode):
    target = deployment_obj["metadata"]["name"]

    def targets(obj):
        ref = obj["spec"]["scaleTargetRef"]
        return ref["name"] == target and ref.get("kind", "Deployment") == "Deployment"

    hs, ss = [h for h in hpas if targets(h)], [s for s in scaled_objects if targets(s)]
    owners = deployment_obj["metadata"].get("ownerReferences", [])
    if owners:
        raise ValueError(
            "default scale target must not be owned by KServe or another parent"
        )
    if mode.startswith("fixed"):
        valid = not hs and not ss
    elif mode == "hpa":
        valid = len(hs) == 1 and not ss and not hs[0]["metadata"].get("ownerReferences")
    elif mode == "keda":
        valid = (
            len(hs) == 1
            and len(ss) == 1
            and any(
                o.get("uid") == ss[0]["metadata"].get("uid")
                and o.get("kind") == "ScaledObject"
                and o.get("controller") is True
                for o in hs[0]["metadata"].get("ownerReferences", [])
            )
        )
    else:
        raise ValueError("unknown writer mode")
    if not valid:
        raise ValueError("replicas writer conflict or incomplete KEDA ownership chain")
    managers = [
        f.get("manager")
        for f in deployment_obj["metadata"].get("managedFields", [])
        if "f:replicas" in f.get("fieldsV1", {}).get("f:spec", {})
    ]
    return dict(
        mode=mode,
        hpas=[h["metadata"]["name"] for h in hs],
        scaled_objects=[s["metadata"]["name"] for s in ss],
        replicas_field_managers=managers,
        limitation="managedFields is history; verify repeated samples and disable GitOps replicas sync",
    )
