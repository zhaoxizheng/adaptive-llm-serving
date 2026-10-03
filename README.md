# Cloud-Native LLM Serving: 22-Week Learning Roadmap

A hands-on project that starts with measured single-GPU autoregressive inference,
progresses through vLLM internals and same-host multi-GPU tensor parallelism, and
then builds a cloud-provider-neutral serving path around Kubernetes Gateway API,
Gateway API Inference Extension (GAIE), llm-d, and vLLM.

The portable data-plane baseline is `Gateway`/`HTTPRoute` + `InferencePool` + an
llm-d Endpoint Picker (EPP) + vLLM. Each logical vLLM replica is one Pod on one
node and may use one or more same-node GPUs with tensor parallelism. HPA or KEDA
scales complete replicas without changing their GPU count or tensor-parallel size;
KServe is evaluated separately as an optional declarative control plane.

This is the roadmap's only production model-execution topology. Independent replicas
may be scheduled on different nodes, while every replica keeps all GPU and TP ranks
co-located on one node.

```text
Client -> Gateway API / HTTPRoute -> GAIE InferencePool -> llm-d EPP
                                                     |- replica A: 1 Pod / 1 node / G GPUs / TP=G
                                                     `- replica B: 1 Pod / 1 node / G GPUs / TP=G

Direct HPA or KEDA-managed HPA -> Deployment replica count only
Total allocated GPUs = replica count x G
```

Start with the [22-week learning roadmap](docs/learning-roadmap.md), then use the
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

### Weeks 16–22 Overview

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
| 20 | HPA/KEDA autoscaling and observability | Metric contract, cold-start/failure timelines, and allocated/billed GPU-hours |
| 21 | Cloud implementation mapping and portability | GKE live run plus evidence-backed mappings for other providers |
| 22 | Capstone: held-out, failure, rollout, and runbook | Repeated A/B results, fault matrix, compatibility table, and rollback runbook |

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

- Weeks 1–13 use the single NVIDIA L4 baseline. Week 14 includes one same-host
  dual-GPU TP cell; if that hardware is unavailable, mark it `deferred`.
- Week 15 and performance cells in Weeks 16–22 require two complete replica slots.
  If one replica uses `G` same-node GPUs with `TP=G`, the two-replica experiments
  require `2 × G` GPUs, with each group of `G` co-located on one node. A CPU cluster
  may validate CRDs and reconciliation only.
- Prefer stable on-demand capacity for controlled multi-replica comparisons. If Spot
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
make run-week01 PYTHON=.venv/bin/python
```

`run-week01` captures the environment, validates the immutable model snapshot,
persists structured smoke evidence, runs or resumes the full matrix, saves a
combined log, and generates both figures. Do not write the report on the billable
GPU VM. Sync the evidence and stop the VM first.

Back on the Mac, download the results and stop compute billing:

```bash
scripts/sync_results_from_gcp.sh .
scripts/gcp_vm.sh stop
scripts/gcp_vm.sh status
```

On the Mac, install the lightweight verification dependencies if they are not
already available, write the numerical conclusions in `reports/week01.md` from the
synced raw data and figures, then run the offline evidence gate. The verifier checks
that the current experiment code matches the code used on the GPU; report-only
edits do not change that identity.

```bash
python3.12 -m venv .verify-venv
.verify-venv/bin/python -m pip install PyYAML==6.0.2
make verify PYTHON=.verify-venv/bin/python
```

The stopped VM does not incur compute charges, but its persistent disk continues to incur storage charges. Delete the VM after preserving results when it is no longer needed.

## Spot Resume Behavior

`make benchmark` uses `results/week01/raw/kv_cache.csv` as its checkpoint:

- each completed case is written through a temporary file and atomically replaced;
- the file is flushed to disk before the next case starts;
- completed `(prompt_tokens, output_tokens, repeat, use_cache)` cases are skipped on restart;
- an immutable run ID binds every row to the scientific config, clean Git commit,
  pinned model revision, GPU identity, Python, PyTorch, Transformers, CUDA, and driver;
- resume validates the CSV and all identities before accepting any completed case.

If GCP stops the VM, start it again and rerun the same command. To intentionally change the benchmark configuration, archive or remove the old CSV first.

## Commands

| Command | Purpose |
|---|---|
| `make install` | Install runtime dependencies |
| `make check-env` | Capture GPU and software versions |
| `make prepare-model` | Download the pinned model snapshot and hash its files |
| `make smoke` | Run one short generation and save structured evidence |
| `make benchmark` | Compare KV cache enabled and disabled, resuming if interrupted |
| `make report` | Generate charts from raw benchmark data |
| `make run-week01` | Run the complete evidence-producing Week 1 workflow |
| `make verify` | Verify all Week 1 artifacts offline without running CUDA |
| `make test` | Run unit tests |
| `make lint` | Run Ruff static checks |

Override the configuration when needed:

```bash
make benchmark CONFIG=configs/week01.yaml PYTHON=.venv/bin/python
```

## Experiment Contract

Every performance result must include:

- Git commit
- clean/dirty state and immutable run ID
- configuration fingerprint
- GPU model and count
- driver, CUDA, PyTorch, and Transformers versions
- model identifier, immutable revision, and snapshot file hashes
- dtype
- prompt and output token counts
- warmup and repetition counts
- raw per-run results

Week 1 standardizes on Python 3.12, PyTorch 2.8.0 with the CUDA 12.8 wheel,
Transformers 4.46.3, and a pinned Qwen model commit. Direct dependencies are pinned;
the full post-install dependency freeze is retained as audit evidence and bound to
the run identity. The actual GCE image, GPU capability, driver, and CUDA runtime are
also retained. The freeze is not a hash-locked cross-platform environment lock.

Do not compare runs across different GPU models as if they were controlled results.
Resume one CSV only when the runner accepts the same source, machine ID/GCE instance
ID, dependency freeze, GPU, CUDA, driver, and model snapshot identities.

## Target Repository Layout

The later-week directories below are planned deliverables and are not claims that the
corresponding manifests, experiments, or reports already exist.

```text
benchmark/           Workload definitions, runners, and analysis
configs/             Versioned experiment configurations
dashboards/          Cross-layer observability views
deploy/              vLLM, Gateway/GAIE, llm-d, autoscaling, and provider manifests
docs/                Weekly plans/references, architecture, portability, and ADRs
reports/             Written experiment conclusions and runbooks
results/             Raw records, timelines, and environment manifests by week
scripts/             GCP lifecycle, deployment, validation, and transfer helpers
src/                 Inference and analysis code
tests/               CPU-safe unit tests and configuration checks
```

## Current Scope

Week 1 intentionally uses Hugging Face Transformers rather than vLLM. The goal is to understand and measure the underlying prefill/decode loop before introducing continuous batching, paged KV cache, scheduling, and serving infrastructure.

