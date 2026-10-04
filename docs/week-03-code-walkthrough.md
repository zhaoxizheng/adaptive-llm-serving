# Week 3 Code Walkthrough

Week 3 has two deliberately separate request-level batching systems. The `fake`
backend is a deterministic CPU-only simulation: it never imports Torch or sleeps and
writes only to `results/week03-simulation/`. The `hf` backend is the measured path:
it replays arrivals on `time.monotonic_ns()`, feeds a bounded admission queue, and
executes batches on one background GPU worker while the producer remains live.

## Data flow

```text
validated Week 2 batch-size evidence
  -> week03_calibration.py selects measured batch-1 median requests/s
seed + measured calibration artifact
  -> workload.py writes one portable trace per (load, repeat)
  -> fake: virtual clock + configured teaching service curve
  -> hf: monotonic producer + bounded admission + scheduler + single worker
  -> per-case events.csv + batches.csv + complete.json (written last)
  -> aggregate CSVs -> analyze_week03.py -> four figures and summary.csv
  -> verify_week03.py checks the complete evidence graph
```

Persisted trace timestamps are offsets from case start. The HF producer maps them to
`time.monotonic_ns() + offset`; the fake runner advances directly to the next event.
All policies consume the same trace, so randomness never occurs while
comparing schedulers. The configured duration includes warmup. Warmup requests affect
queue state but are excluded from reported rates and percentiles.
Prompt token IDs are also trace-bound: both Week 3 HF execution and Week 4 replay use
the exact-length encoding of `Trace request <request_id>. <base prompt>`. A request
therefore keeps the same content when policy decisions change its batch membership.

## Exact scheduler semantics

- `no_batching`: FIFO singleton batches dispatch at admission time.
- `fixed_window`: boundaries are anchored to case time zero; a request waits for the
  next boundary even when it is the only queued request.
- `size_or_time`: the oldest admitted request owns the deadline; reaching batch size
  flushes immediately, otherwise that deadline flushes the current partial batch.
- An arrival at exactly a boundary or deadline is submitted before the flush and can
  join it. Equal-time arrivals retain trace ordinal order.
- The producer timestamps and publishes every due arrival without waiting for queue
  capacity. Queue-full requests wait in coordinator-owned FIFO admission state, each
  with an independent deadline measured from its own observed arrival. This keeps a
  saturated request from delaying observation of later trace arrivals.
- Shutdown flushes a final partial batch. A queue-full request that reaches its own
  bounded admission deadline writes an explicit rejected event with actual monotonic
  observation and terminal timestamps.

Golden size-or-time example (`max_batch_size=3`, `max_wait=5 ms`):

```text
arrivals:     A@0, B@2, C@5, D@8 ms
batch 0:      [A, B, C] dispatches @5 ms (arrival C is processed first; size wins)
batch 1:      [D]       dispatches @13 ms (D's oldest-request deadline)
```

## Evidence and resume contract

Each request row is bound to the run, metadata fingerprint, scientific config,
calibration, case, and trace. It records scheduled and observed arrival, admission, dispatch, worker
start, first token, finish/terminal status, and recomputable duration columns. Rejected
rows have no admission or GPU lifecycle. Failed rows contain the valid lifecycle prefix
that occurred before failure. Batch rows contain canonical JSON membership, trigger,
fill ratio, worker timestamps, queue depth, and status. The validator cross-checks both
files and rejects duplicate membership or timestamp disagreement.

Official cases are staged under `results/week03/raw/cases/<case-id>/`; simulation and
smoke cases use physically separate roots. `complete.json` is
written last and hashes both CSVs. Resume accepts only an intact, validated case; an
interrupted directory is rerun as a whole case. Aggregate CSVs are atomically rebuilt
from complete cases in deterministic case order.

## Bounded experiment profiles

The primary profile has 55 cases, or 1.83 raw GPU-hours at 120 seconds per case. It
contains the five-load, three-policy, three-repeat core plus a focused 0.75-load batch
size/delay sweep. The optional extended profile has 87 deduplicated cases (2.9 raw
GPU-hours) and should be run only when the primary results motivate it. `no_batching`
is normalized to batch size 1/delay 0 rather than redundantly repeated for every batch
parameter. The smoke profile runs exactly 20 trace requests for each policy.

## Commands

```bash
# CPU-only simulation; never official evidence
PYTHON=python3 PROFILE=primary BACKEND=fake bash scripts/run_week03.sh

# Run a bounded number of incomplete cases, then resume later
PYTHON=python3 PROFILE=primary BACKEND=fake MAX_CASES=3 bash scripts/run_week03.sh

# Build capacity evidence from the completed, validated Week 2 batch-size run
.venv/bin/python -m src.week03_calibration \
  --week3-config configs/week03.yaml --week2-config configs/week02.yaml

# Real-time L4 smoke; isolated under results/week03-smoke
PYTHON=.venv/bin/python PROFILE=smoke BACKEND=hf bash scripts/run_week03.sh

# Formal primary run, then verifier (hf + primary only)
PYTHON=.venv/bin/python PROFILE=primary BACKEND=hf bash scripts/run_week03.sh
.venv/bin/python -m scripts.verify_week03 --config configs/week03.yaml
```

Formal verification rejects fake or smoke output, dirty source, placeholder/manual
calibration, a different GPU/software runtime from calibration, missing CUDA,
dependency-freeze, environment, or model-snapshot provenance, failed HF batches,
mixed row identity, stale aggregates, and non-reproducible summary/analysis artifacts.
It does not create or imply GPU evidence on a CPU-only host. HF service time starts
before tokenization and includes preprocessing, H2D, prefill, and decode.

The four plots use exact filenames from the plan. `achieved_throughput_rps` uses
completed measurement requests over the declared post-warmup measurement interval.
`drain_inclusive_throughput_rps` separately divides by wall time through the last
terminal request, making overload drain visible without changing the primary
denominator. Completion, rejection, and failure rates use all attempted measurement
requests; latency percentiles use completed requests only.
