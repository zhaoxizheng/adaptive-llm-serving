# Adaptive LLM Serving

A hands-on learning project that starts with measured single-GPU autoregressive inference, progresses through vLLM internals and multi-replica serving, and ends with SLO-aware routing and autoscaling on AIBrix.

The first twenty weekly milestones cover measured autoregressive inference, batching,
single-instance vLLM serving and tuning, the vLLM request path and memory lifecycle,
framework/system/kernel profiling, multi-replica baselines, AIBrix autoscaling and
cache-aware routing, then the capstone experiment design and minimal routing policy.

Start with the [22-week learning roadmap](docs/learning-roadmap.md), then use the weekly plans and reading lists:

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

### Next Four Learning Weeks

This extends the documented sequence through Week 20; it does not imply that
Weeks 1–16 are complete. Week numbers are prerequisite-based milestones, not
calendar dates. Start Week 17 after the Week 16 evidence is available, and shift
later weeks if a prerequisite is blocked. Each week budgets about 11 hours.

| Week | Focus | Deliverable |
|---|---|---|
| 17 | Inference-aware autoscaling with fixed routing | Metric contract, scaling timeline, SLO and allocated/billed GPU-hours |
| 18 | Cache-aware routing with fixed replicas | Locality/load comparison and KV event consistency evidence |
| 19 | Capstone experiment design and offline policy | Frozen SLO/data splits, minimal policy contract and replay tests |
| 20 | Minimal AIBrix routing integration | Correctness tests, streaming smoke and fixed-replica A/B with ablation |

Performance validation requires two real GPU slots; Week 19 is primarily local
analysis. Unavailable hardware is a blocked or deferred experiment, not a
successful CPU substitute. The deliverable paths are planned artifacts, not
claims of implemented features or measured gains. Combined routing/autoscaling
experiments and final held-out validation remain in Weeks 21–22.

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
configs/             Versioned experiment configurations
docs/                Learning plans, references, and GCP environment guide
reports/             Written experiment conclusions
results/week01/raw/  Raw benchmark records
scripts/             GCP lifecycle, environment, and transfer helpers
src/                 Inference and analysis code
tests/               Unit tests that do not require a GPU
```

## Current Scope

Week 1 intentionally uses Hugging Face Transformers rather than vLLM. The goal is to understand and measure the underlying prefill/decode loop before introducing continuous batching, paged KV cache, scheduling, and serving infrastructure.

