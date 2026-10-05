# Week 19: KServe LLMInferenceService resource audit

Status: **not_run**. Code and CPU tests do not establish live cluster or GPU results.

Run instructions: [Chinese walkthrough](../docs/week-19-code-walkthrough.md).

## Run identity

- Session directories / immutable source identity: pending
- Config/baseline/artifact hashes: pending
- Cluster, CRD/controller/runtime versions and images: pending
- Model/revision, G GPUs per replica, TP=G, Pod/node/ranks: pending
- Workload seeds, offered rates, cache treatment and SLO: pending

## Evidence and conclusions

| Question | Evidence | Result / limitation |
|---|---|---|
| Installed alpha API versions and schema differences | pending | not observed |
| Observed resource DAG and UID ownership | pending | not observed |
| Composition/defaulting and field provenance | pending | not observed |
| Controller Ready versus actual first token | pending | not observed |
| Mutation, invalid dependency and NotReady outcomes | pending | not observed |
| Deletion cascade versus shared resources | pending | not observed |
| Replicas writer and Week 20 ordinary Deployment handoff | pending | not observed |

## Cells and missing evidence

Record every planned repeat, failed/blocked cell, client overload, fault-only sample and missing telemetry. Do not discard failed requests from the offered-load SLO denominator.

## Recovery and retained resources

- Restored baseline and generation smoke: pending
- Results synchronized and checksums verified: pending
- GPU nodes, disks, LoadBalancers, public IPs and controllers: pending
- Retained resources and billing boundary: pending
