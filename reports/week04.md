# Week 4: vLLM as a Service

> Evidence status: **NOT YET MEASURED**. GPU compatibility: unverified until
> the pinned runtime completes offline, non-streaming, and streaming generation
> smoke tests on the target NVIDIA L4. This template intentionally contains no
> benchmark claims; `scripts/verify_week04.py` rejects it until real evidence is added.

## Question

At what single-instance load does vLLM continuous batching stop improving useful
throughput under explicit TTFT, TPOT, and error-rate limits, and how does that
point compare with Week 3 request-level batching?

## Environment and Version Contract

- Target: GCP `g2-standard-4` Spot, one NVIDIA L4 24 GB
- Model: `Qwen/Qwen2.5-0.5B-Instruct`
- Revision: `7ae557604adf67be50417f59c2c2f167def9a775`
- vLLM: `0.10.2` in the separate `.venv-vllm` environment
- Dtype: BF16
- Endpoint: loopback only; remote access uses SSH port forwarding
- Runtime, dependency, CLI-help, Git, and model evidence: add links after the GPU run

## Method

The primary matrix fixes the model, immutable revision, dtype, greedy decoding,
request count, and token shapes. It runs three repeats of closed-loop concurrency
`[1, 2, 4, 8, 16]` over four fixed workloads and open-loop rates
`[1, 2, 4, 8] requests/s` for the balanced workload. A 20-request benchmark
smoke must pass first. Interrupted cases remain incomplete evidence and are never
stitched into a formal run.

Client TTFT and server queue time are separate measurements: TTFT includes client,
HTTP, serialization, server tokenization, queueing, compute, and first-chunk
transport; queue time represents only the server-side waiting interval exposed by
the captured vLLM metric contract.

## Results

TBD_AFTER_GPU_RUN: add the run ID, compact numeric table, error/timeout counts,
and all four generated figures.

- `concurrency-vs-throughput.png`
- `concurrency-vs-p99-ttft.png`
- `request-rate-vs-queue-time.png`
- `week03-vs-vllm-balanced.png`

## Week 3 Controlled Comparison

TBD_AFTER_GPU_RUN: compare only balanced 256-input/64-output-token cases with the
same GPU, model revision, BF16 dtype, request count, warmup, arrival trace, timeout,
and success criteria. Explain request-level batch completion waste versus vLLM
iteration-level scheduling without treating the two internal queue definitions as
equivalent.

## Operating Point

Configured SLO: P99 TTFT <= 2000 ms, P99 TPOT <= 100 ms, and error rate = 0%.
TBD_AFTER_GPU_RUN: select the highest stable request rate satisfying all three and
record its full server arguments.

## Limitations

- One small model and one L4 do not establish multi-model or multi-GPU behavior.
- Synthetic fixed-token requests do not measure response quality or natural prompts.
- The official client aggregate and `/metrics` have different observation boundaries.
- Spot interruption and failed cases must remain visible rather than being discarded.

## Resource Cleanup

TBD_AFTER_GPU_RUN: record VM stop time and audit persistent disks, reserved/external
addresses, load balancers, and node pools in `results/week04/gpu-resource-audit.json`.
