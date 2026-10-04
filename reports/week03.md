# Week 3 Report: Dynamic Request Batching, Queueing, and Tail Latency

> This is an evidence template. Replace every `TODO_FROM_EVIDENCE` value only after
> running the complete matrix and reading `results/week03/summary.csv`.

## Run Identity

- Run ID: `RUN_ID_FROM_METADATA`
- Metadata fingerprint: TODO_FROM_EVIDENCE
- Measured calibration ID: TODO_FROM_EVIDENCE
- Backend and GPU: TODO_FROM_EVIDENCE
- Calibrated capacity: TODO_FROM_EVIDENCE requests/s
- Fixed request shape: prompt 256, output 64
- Measurement window: the configured duration includes warmup; warmup arrivals are
  replayed but excluded from rates and percentiles.

## Method

The formal evidence contract accepts only `backend=hf`, `profile=primary` from clean
committed source with CUDA/runtime, dependency-freeze, model-snapshot, environment,
and measured Week 2 calibration provenance. Fake simulation artifacts live in a
separate root and cannot satisfy formal verification.

One seeded Poisson trace was persisted before serving for each `(load, repeat)` and
reused byte-for-byte by `no_batching`, `fixed_window`, and `size_or_time`. Timestamps
are case-relative monotonic nanoseconds. The system has one bounded admission queue,
one request-level batch scheduler, and one worker. A batch cannot accept new requests
after dispatch. Arrival lag is reported separately from scheduler queueing delay.

## Results

| Policy | Offered load | Throughput | P95 TTFT | P99 TTFT | Completion | Rejection | Failed |
|---|---:|---:|---:|---:|---:|---:|---:|
| TODO_FROM_EVIDENCE | TODO_FROM_EVIDENCE | TODO_FROM_EVIDENCE requests/s | TODO_FROM_EVIDENCE ms | TODO_FROM_EVIDENCE ms | TODO_FROM_EVIDENCE% | TODO_FROM_EVIDENCE% | TODO_FROM_EVIDENCE% |

![Offered load versus throughput](../results/week03/figures/offered-load-vs-throughput.png)

![Offered load versus tail TTFT](../results/week03/figures/offered-load-vs-p95-p99-ttft.png)

![Window versus fill and queue delay](../results/week03/figures/batch-window-vs-fill-and-queue-delay.png)

## Overload and Tail Latency

TODO_FROM_EVIDENCE: identify the first sustained-queue point for each policy, compare
P50 with P99, report P99 arrival lag, and explain all rejection and failed-request
denominators. State whether any configuration improved throughput while worsening P99.

![Queue depth during overload](../results/week03/figures/queue-depth-over-time-overload.png)

## Limitations

This scheduler implements request-level dynamic batching. Every batch executes as a
fixed group until all requests finish. HF service time includes CPU preprocessing,
H2D, prefill, and decode. It is not continuous batching, does not refill
decode slots at iteration boundaries, and does not implement PagedAttention, KV block
allocation, or preemption. Fake-mode values validate semantics and plots but are not
GPU performance claims; smoke output is also not accepted as primary evidence.

## Week 4 Hypotheses

1. vLLM continuous batching will waste fewer decode slots than request-level batching
   when output lengths differ.
2. At the same offered load, vLLM will move the queue-instability boundary upward.
3. vLLM server queue time and this experiment's client-observed waiting time will not
   be directly interchangeable and must remain separate metrics.
