# Cloud-Native LLM Serving: 26-Week Learning Roadmap

A hands-on project that starts with measured single-GPU autoregressive inference,
progresses through vLLM internals and multi-replica serving, and then builds a
cloud-provider-neutral serving path around Kubernetes Gateway API, Gateway API
Inference Extension (GAIE), llm-d, and vLLM. The final block compares Kubernetes
workload abstractions for multi-node inference instead of assuming one platform is
the universal answer.

The portable data-plane baseline is `Gateway`/`HTTPRoute` + `InferencePool` + an
llm-d Endpoint Picker (EPP) + vLLM. HPA or KEDA supplies the primary autoscaling
path; KServe is evaluated as an optional declarative control plane. AIBrix gateway
and autoscaling remain outside the 26-week core; Week 26 only maps its optional
`RayClusterFleet` abstraction after the direct LeaderWorkerSet and KubeRay paths.

Start with the [26-week learning roadmap](docs/learning-roadmap.md), then use the
weekly execution plans and reading lists:

- [Week 1 execution plan](docs/week-01-plan.md) and [references](docs/week-01-references.md)
- [Week 2 execution plan](docs/week-02-plan.md) and [references](docs/week-02-references.md)
- [Week 3 execution plan](docs/week-03-plan.md) and [references](docs/week-03-references.md)
- [Week 4 execution plan](docs/week-04-plan.md) and [references](docs/week-04-references.md)
- [Week 5 execution plan](docs/week-05-plan.md) and [references](docs/week-05-references.md)
- [Week 6 execution plan](docs/week-06-plan.md) and [references](docs/week-06-references.md)
- [Week 7 execution plan](docs/week-07-plan.md) and [references](docs/week-07-references.md)
- [Week 8 execution plan](docs/week-08-plan.md) and [references](docs/week-08-references.md)
- [Week 9 execution plan](docs/week-09-plan.md) and [references](docs/week-09-references.md)
- [Week 10 execution plan](docs/week-10-plan.md) and [references](docs/week-10-references.md)
- [Week 11 execution plan](docs/week-11-plan.md) and [references](docs/week-11-references.md)
- [Week 12 execution plan](docs/week-12-plan.md) and [references](docs/week-12-references.md)
- [Week 13 execution plan](docs/week-13-plan.md) and [references](docs/week-13-references.md)
- [Week 14 execution plan](docs/week-14-plan.md) and [references](docs/week-14-references.md)
- [Week 15 execution plan](docs/week-15-plan.md) and [references](docs/week-15-references.md)
- [Week 16 execution plan](docs/week-16-plan.md) and [references](docs/week-16-references.md)
- [Week 17 execution plan](docs/week-17-plan.md) and [references](docs/week-17-references.md)
- [Week 18 execution plan](docs/week-18-plan.md) and [references](docs/week-18-references.md)
- [Week 19 execution plan](docs/week-19-plan.md) and [references](docs/week-19-references.md)
- [Week 20 execution plan](docs/week-20-plan.md) and [references](docs/week-20-references.md)
- [Week 21 execution plan](docs/week-21-plan.md) and [references](docs/week-21-references.md)
- [Week 22 execution plan](docs/week-22-plan.md) and [references](docs/week-22-references.md)
- [Week 23 execution plan](docs/week-23-plan.md) and [references](docs/week-23-references.md)
- [Week 24 execution plan](docs/week-24-plan.md) and [references](docs/week-24-references.md)
- [Week 25 execution plan](docs/week-25-plan.md) and [references](docs/week-25-references.md)
- [Week 26 execution plan](docs/week-26-plan.md) and [references](docs/week-26-references.md)

### Weeks 16–26 Overview

Week numbers are prerequisite-based milestones, not calendar dates. The roadmap
does not imply that earlier weeks are complete. Start each milestone only when its
input evidence exists, and move blocked GPU experiments instead of replacing them
with incomparable CPU results. Each week budgets about 11 hours.

| Week | Focus | Deliverable |
|---|---|---|
| 16 | Gateway API v1 L7 Baseline | Auditable `Gateway`/`HTTPRoute` matching, streaming, attribution, and drain evidence |
| 17 | GAIE `InferencePool` v1 and Reference EPP | Standard endpoint-selection data path, conformance boundary, and EPP failure semantics |
| 18 | llm-d Router/EPP: Load-aware and Precise Prefix-aware Routing | Fixed-replica routing comparison with locality/load/staleness evidence |
| 19 | KServe `LLMInferenceService` control plane | Alpha reconciliation and generated-resource audit |
| 20 | HPA/KEDA autoscaling and observability | Metric contract, cold-start/failure timelines, and allocated/billed GPU-hours; WVA optional |
| 21 | Cloud implementation mapping and portability | GKE live run plus evidence-backed mappings for other providers |
| 22 | Capstone: held-out, failure, rollout, and runbook | Repeated A/B results, fault matrix, compatibility table, and rollback runbook |
| 23 | Multi-node vLLM and runtime contract | TP/PP/DP/EP runtime and communication decision table |
| 24 | LeaderWorkerSet + Kueue | Group lifecycle, gang admission, topology, and recovery evidence |
| 25 | Ray + KubeRay | `RayService`, placement-group, and Kubernetes/runtime state evidence |
| 26 | Same-hardware LWS vs KubeRay ADR | Lifecycle/cost decision; optional `RayClusterFleet` manifest mapping |

### API, Cloud, and Evidence Boundaries

- GAIE `InferencePool` has a stable `v1` API. That does not imply every Gateway
  implementation or every related inference API is generally available.
- KServe `LLMInferenceService` remains an alpha API. Pin the chosen KServe
  release and verify its installed CRD schema rather than treating it as a stable
  portability contract.
- GKE is the managed-cloud path exercised end to end. Other providers are mapped
  from official APIs and documentation unless a weekly report explicitly records a
  live run. The roadmap makes no provider adoption or market-share claim.
- Deliverable paths and example conclusions are plans, not claims that features
  have been implemented or that a benchmark has already shown a gain.

### Hardware and Blocked Experiments

- Weeks 1–14 use the single NVIDIA L4 baseline. Week 15 and performance cells in
  Weeks 16–22 require two independently schedulable, same-model GPU slots. A CPU
  cluster may validate CRDs and reconciliation only.
- Weeks 23–26 require at least two same-zone GPU nodes for the mandatory
  multi-node lifecycle experiments. L4 over ordinary cloud TCP is useful for
  correctness and orchestration evidence, not production collective-performance
  claims.
- Meaningful cross-node TP/EP performance conclusions require suitable model
  scale, matched accelerators, known topology, and high-bandwidth GPU networking.
  When those resources are unavailable, mark the affected cell `blocked` or
  `deferred`; retain the failure record and continue with independent analysis.
- Prefer stable on-demand capacity for controlled multi-node comparisons. If Spot
  is used, record preemption separately and never merge it into the steady-state
  result. Stop GPU nodes and audit disks, load balancers, and addresses after each
  experiment window.

### Reference Reuse

Each external source appears once in the numbered weekly reading lists, at its
first introduction. Later weeks link to that week's reference number and state
only the new question or section to revisit. Reused material is not counted as
new reading; changing a title, fragment, or version URL does not make it a new
source. Distinct design/API documents on the same topic are included only when
they add a specific missing concept. Keep reading schedules and cross-week
reference numbers consistent when removing duplicate entries.

## Week 1 Architecture

```text
Mac M3 Pro
├── code, Git, analysis, and reports
└── gcloud SSH / SCP
          ↓
GCP Spot VM: g2-standard-4
├── 1 × NVIDIA L4 24 GB
├── 100 GB persistent boot disk
├── Ubuntu + NVIDIA driver + PyTorch + Transformers
├── measured greedy decode loop
└── incremental raw CSV + environment metadata + figures
```

The Mac is the development machine. Model loading and measured inference run only on the GCP VM. The Spot VM can be preempted, so every completed benchmark case is atomically persisted and a repeated `make benchmark` resumes the incomplete matrix.

## Quick Start on GCP Spot

Complete the billing, API, and quota checks in [the GCP Spot setup guide](docs/gcp-spot-setup.md). No billable resource is created until the `create` command is run.

On the Mac:

```bash
export GCP_PROJECT_ID=your-project-id
export GCP_ZONE=us-central1-a

scripts/gcp_vm.sh create
scripts/upload_to_gcp.sh
scripts/gcp_vm.sh ssh
```

On the VM:

```bash
cd ~/adaptive-llm-serving
bash scripts/bootstrap_gcp.sh .
make smoke PYTHON=.venv/bin/python
make benchmark PYTHON=.venv/bin/python
make report PYTHON=.venv/bin/python
```

Back on the Mac, download the results and stop compute billing:

```bash
scripts/sync_results_from_gcp.sh ./gcp-results
scripts/gcp_vm.sh stop
```

The stopped VM does not incur compute charges, but its persistent disk continues to incur storage charges. Delete the VM after preserving results when it is no longer needed.

## Spot Resume Behavior

`make benchmark` uses `results/week01/raw/kv_cache.csv` as its checkpoint:

- each completed case is written through a temporary file and atomically replaced;
- the file is flushed to disk before the next case starts;
- completed `(prompt_tokens, output_tokens, repeat, use_cache)` cases are skipped on restart;
- a configuration fingerprint prevents accidentally combining different experiment matrices.

If GCP stops the VM, start it again and rerun the same command. To intentionally change the benchmark configuration, archive or remove the old CSV first.

## Commands

| Command | Purpose |
|---|---|
| `make install` | Install runtime dependencies |
| `make check-env` | Capture GPU and software versions |
| `make smoke` | Run one short measured generation |
| `make benchmark` | Compare KV cache enabled and disabled, resuming if interrupted |
| `make report` | Generate charts from raw benchmark data |
| `make test` | Run unit tests |
| `make lint` | Run Ruff static checks |

Override the configuration when needed:

```bash
make benchmark CONFIG=configs/week01.yaml PYTHON=.venv/bin/python
```

## Experiment Contract

Every performance result must include:

- Git commit
- configuration fingerprint
- GPU model and count
- driver, CUDA, PyTorch, and Transformers versions
- model identifier and revision
- dtype
- prompt and output token counts
- warmup and repetition counts
- raw per-run results

Do not compare runs across different GPU models as if they were controlled results. Resume one CSV only on the same VM and GPU type.

## Repository Layout

```text
benchmark/           Workload definitions, runners, and analysis
configs/             Versioned experiment configurations
dashboards/          Cross-layer observability views
deploy/              vLLM, Gateway/GAIE, llm-d, autoscaling, LWS, and KubeRay manifests
docs/                Weekly plans/references, architecture, portability, and ADRs
reports/             Written experiment conclusions and runbooks
results/             Raw records, timelines, and environment manifests by week
scripts/             GCP lifecycle, deployment, validation, and transfer helpers
src/                 Inference and analysis code
tests/               CPU-safe unit tests and configuration checks
```

## Current Scope

Week 1 intentionally uses Hugging Face Transformers rather than vLLM. The goal is to understand and measure the underlying prefill/decode loop before introducing continuous batching, paged KV cache, scheduling, and serving infrastructure.

