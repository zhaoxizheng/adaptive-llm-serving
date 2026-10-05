import copy
import hashlib
from pathlib import Path

import pytest
import yaml

from src.platform_contract import (
    artifact,
    check_pool_schema,
    data_plane,
    load_study,
    pool_resource,
    require_subset,
    scaling_resources,
    scoped_resources,
    writer_check,
)


def config(week=17):
    suffix = {
        17: "inferencepool",
        18: "routing",
        19: "resource-audit",
        20: "autoscaling",
    }[week]
    return load_study(f"configs/week{week}-{suffix}.yaml")


def test_pool_failure_modes_and_complete_replica():
    cfg, base, lab, _ = config()
    omitted = pool_resource(cfg, lab, None)
    assert "failureMode" not in omitted["spec"]["endpointPickerRef"]
    assert (
        pool_resource(cfg, lab)["spec"]["endpointPickerRef"]["failureMode"]
        == "FailClose"
    )
    dep, _, metrics, pool, route = data_plane(cfg, base, lab, {})
    assert dep["spec"]["replicas"] == 2
    engine = dep["spec"]["template"]["spec"]["containers"][0]
    assert engine["resources"]["limits"]["nvidia.com/gpu"] == base["gpus_per_replica"]
    assert pool["spec"]["targetPorts"] == [{"number": 8081}]
    assert metrics["spec"]["ports"][0]["targetPort"] == 8000
    assert route["spec"]["rules"][0]["backendRefs"][0]["kind"] == "InferencePool"


def test_artifact_is_immutable(tmp_path):
    path = tmp_path / "release.yaml"
    path.write_text("fixed release")
    lock = {
        "artifacts": {
            "crd": {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        }
    }
    assert artifact(lock, "crd") == b"fixed release"
    path.write_text("changed release")
    with pytest.raises(ValueError, match="checksum"):
        artifact(lock, "crd")
    with pytest.raises(ValueError, match="supply"):
        artifact(lock, "missing")


def test_pool_schema_requires_api_default():
    schema = {
        "properties": {
            "spec": {
                "properties": {
                    "selector": {},
                    "targetPorts": {},
                    "endpointPickerRef": {
                        "properties": {
                            "failureMode": {
                                "default": "FailClose",
                                "enum": ["FailClose", "FailOpen"],
                            }
                        }
                    },
                }
            }
        }
    }
    crd = {
        "kind": "CustomResourceDefinition",
        "spec": {
            "names": {"kind": "InferencePool"},
            "versions": [
                {"name": "v1", "served": True, "schema": {"openAPIV3Schema": schema}}
            ],
        },
    }
    assert check_pool_schema([crd]) == schema
    schema["properties"]["spec"]["properties"]["endpointPickerRef"]["properties"][
        "failureMode"
    ]["default"] = "FailOpen"
    with pytest.raises(ValueError, match="failureMode"):
        check_pool_schema([crd])


def test_scaler_metric_denominator_and_bounds():
    cfg, _, lab, _ = config(20)
    hpa = scaling_resources(cfg, lab, "hpa")[0]["spec"]
    so = scaling_resources(cfg, lab, "keda")[0]["spec"]
    assert hpa["metrics"][0]["object"]["target"]["type"] == "AverageValue"
    assert so["triggers"][0]["metricType"] == "AverageValue"
    assert so["triggers"][0]["metadata"]["ignoreNullValues"] == "false"
    assert so["triggers"][0]["metadata"]["query"] == cfg["metric"]["query"]
    assert hpa["maxReplicas"] == so["maxReplicaCount"] == 2
    assert hpa["scaleTargetRef"] == so["scaleTargetRef"]
    with pytest.raises(ValueError):
        scaling_resources(cfg, lab, "zero")


def test_unique_writer_distinguishes_generated_hpa():
    dep = {"metadata": {"name": "vllm"}}
    cfg, _, lab, _ = config(20)
    hpa = scaling_resources(cfg, lab, "hpa")[0]
    so = scaling_resources(cfg, lab, "keda")[0]
    so["metadata"]["uid"] = "s1"
    assert writer_check(dep, [], [], "fixed_2")
    assert writer_check(dep, [hpa], [], "hpa")
    with pytest.raises(ValueError):
        writer_check(dep, [hpa], [so], "keda")
    hpa["metadata"]["ownerReferences"] = [
        dict(uid="s1", kind="ScaledObject", controller=True)
    ]
    assert writer_check(dep, [hpa], [so], "keda")
    with pytest.raises(ValueError):
        writer_check(dep, [hpa, copy.deepcopy(hpa)], [so], "keda")
    dep["metadata"]["ownerReferences"] = [{"kind": "LLMInferenceService"}]
    with pytest.raises(ValueError, match="parent"):
        writer_check(dep, [], [], "fixed_1")


def test_apply_rejects_cluster_scope_and_unlabelled_objects():
    cfg, base, lab, _ = config()
    bundle = data_plane(cfg, base, lab, {})
    assert scoped_resources(bundle, lab["namespace"])
    bundle[0]["metadata"]["namespace"] = "default"
    with pytest.raises(ValueError, match="namespaced"):
        scoped_resources(bundle, lab["namespace"])


def test_committed_templates_match_renderer():
    cfg, base, lab, _ = config()
    actual = list(
        yaml.safe_load_all(Path("deploy/gaie/base.template.yaml").read_text())
    )
    assert actual == data_plane(cfg, base, lab, {})
    cfg, _, lab, _ = config(20)
    for mode in ("hpa", "keda"):
        actual = list(
            yaml.safe_load_all(
                Path(f"deploy/autoscaling/{mode}/resource.yaml").read_text()
            )
        )
        assert actual == scaling_resources(cfg, lab, mode)


def test_live_treatment_allows_defaults_but_rejects_wrong_pipeline():
    expected = dict(spec=dict(replicas=2), data=dict(pipeline="precise"))
    require_subset(
        expected,
        dict(
            spec=dict(replicas=2, revisionHistoryLimit=10),
            data=dict(pipeline="precise"),
        ),
    )
    with pytest.raises(ValueError, match="mismatch"):
        require_subset(
            expected, dict(spec=dict(replicas=2), data=dict(pipeline="load"))
        )
