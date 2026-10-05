# Serving lab manifests (Weeks 14–16)

These are reviewable templates, not deployed resources. Image placeholders are intentional.
Render from `configs/serving-baseline.yaml` and `configs/cluster-lab.yaml` after freezing measured
model/engine/resource settings and immutable image digests. See the
[Chinese runbook](../docs/week-13-16-runbook.md).

- `vllm/single-node-multigpu/deployment.yaml`: one Pod, two local GPUs, TP=2 smoke.
- `vllm/multireplica.yaml` + `gateway/rr.yaml`: two complete replicas and request-level RR.
- `autoscaling/hpa.yaml`: CPU baseline, min=1/max=2, no change to G or TP.
- `gateway-api/baseline.yaml`: fixed independent backends and Gateway API core v1 resources.
- `gateway/Dockerfile`: CPU-only RR/identity proxy image; record the built digest in the lock.

The manifest builder is `src/cluster_contract.py`. Committed templates illustrate its output;
the runner renders fresh manifests using the supplied locks. Services are ClusterIP, but a
Gateway controller may provision additional infrastructure: review its private/internal policy.
No controller, GPU node pool, public endpoint, or cloud resource is installed by these files.

The experiment namespace must already exist. Switching Week 15 to Week 16 requires removing
the old lab HPA/replicas within the allocated budget. Fixed and HPA cells must not compete for
the same Deployment scale target. Rolling updates use no surge and may temporarily reduce capacity.

Weeks 17–20 add [GAIE](gaie/README.md), [llm-d](llm-d-router/README.md),
[KServe release inputs](kserve/base/README.md), and [HPA/KEDA](autoscaling/README.md).
See the [Chinese runbook](../docs/week-17-20-runbook.md) for runtime preparation.
