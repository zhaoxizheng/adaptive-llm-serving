# Week 20: HPA / KEDA autoscaling

Status: **not_run**. Code and CPU tests do not establish live cluster or GPU results.

Run instructions: [Chinese walkthrough](../docs/week-20-code-walkthrough.md).

## Run identity

- Session directories / immutable source identity: pending
- Config/baseline/artifact hashes: pending
- Cluster, CRD/controller/runtime versions and images: pending
- Model/revision, G GPUs per replica, TP=G, Pod/node/ranks: pending
- Workload seeds, offered rates, cache treatment and SLO: pending

## Evidence and conclusions

| Question | Evidence | Result / limitation |
|---|---|---|
| Unique writer and common Deployment /scale target | pending | not observed |
| Prometheus/adapter/HPA/KEDA query and denominator equivalence | pending | not observed |
| Observed-to-first-token stages and unknown intervals | pending | not observed |
| Burst/ramp/short-burst/drain SLO across independent repeats | pending | not observed |
| Separate Prometheus/adapter/KEDA outages and recovery | pending | not observed |
| Allocated GPU-hours versus billed GPU/node-hours | pending | not observed |
| Selected control path, evidence, limitations and fixed-replica rollback | pending | not observed |

## Cells and missing evidence

Record every planned repeat, failed/blocked cell, client overload, fault-only sample and missing telemetry. Do not discard failed requests from the offered-load SLO denominator.

## Recovery and retained resources

- Restored baseline and generation smoke: pending
- Results synchronized and checksums verified: pending
- GPU nodes, disks, LoadBalancers, public IPs and controllers: pending
- Retained resources and billing boundary: pending
