# Week 18: llm-d load-aware / precise-prefix routing

Status: **not_run**. Code and CPU tests do not establish live cluster or GPU results.

Run instructions: [Chinese walkthrough](../docs/week-18-code-walkthrough.md).

## Run identity

- Session directories / immutable source identity: pending
- Config/baseline/artifact hashes: pending
- Cluster, CRD/controller/runtime versions and images: pending
- Model/revision, G GPUs per replica, TP=G, Pod/node/ranks: pending
- Workload seeds, offered rates, cache treatment and SLO: pending

## Evidence and conclusions

| Question | Evidence | Result / limitation |
|---|---|---|
| Fixed release and rendered plugin order | pending | not observed |
| Load signal units, freshness and legal fallback | pending | not observed |
| Predicted prefix versus actual engine reuse and identity | pending | not observed |
| Shared/hot/low-sharing/mixed SLO and negative results | pending | not observed |
| Stale/missing signals, replacement, churn and recovery | pending | not observed |
| Gateway/EPP CPU, routing overhead and scope of the conclusion | pending | not observed |

## Cells and missing evidence

Record every planned repeat, failed/blocked cell, client overload, fault-only sample and missing telemetry. Do not discard failed requests from the offered-load SLO denominator.

## Recovery and retained resources

- Restored baseline and generation smoke: pending
- Results synchronized and checksums verified: pending
- GPU nodes, disks, LoadBalancers, public IPs and controllers: pending
- Retained resources and billing boundary: pending
