# Week 2: Batch, Length, Throughput, and Memory

> This is an evidence-bound report template. It deliberately contains no claimed
> benchmark result before the L4 run. Replace every `WEEK02` marker from the generated
> raw CSV and summary; the offline verifier rejects a report with any marker remaining.

<!-- WEEK02-EVIDENCE
run_id=REPLACE_WITH_RUN_ID
raw_sha256=REPLACE_WITH_64_HEX
summary_sha256=REPLACE_WITH_64_HEX
analysis_sha256=REPLACE_WITH_64_HEX
smoke_sha256=REPLACE_WITH_64_HEX
figure_sha256.prompt-length-vs-ttft.png=REPLACE_WITH_64_HEX
figure_sha256.output-length-vs-e2e-latency.png=REPLACE_WITH_64_HEX
figure_sha256.batch-size-vs-token-throughput.png=REPLACE_WITH_64_HEX
figure_sha256.batch-size-vs-latency-memory.png=REPLACE_WITH_64_HEX
-->

## Question

How do prompt length, output length, and static batch size change prefill latency,
decode latency, aggregate throughput, and CUDA memory for cache-on greedy generation?

## Environment

<!-- WEEK02: cite run_id and summarize results/week02/environment.json -->

- Run ID: pending measured run
- Source commit: pending measured run
- GPU, driver, and CUDA: pending measured run
- Model, revision, and dtype: `Qwen/Qwen2.5-0.5B-Instruct`, immutable revision,
  `bfloat16` (confirm these against metadata before removing this marker)

## Metric Contract

| Metric | Week 2 boundary | Tokenizer included? |
|---|---|---|
| `preprocessing_ms` | Tokenize, exact-length construction, padding, mask, CPU tensors | Yes |
| `h2d_ms` | Copy input IDs and attention mask to the GPU | No |
| `gpu_ttft_ms` | Synchronized first model forward only | No |
| `e2e_ttft_ms` | Preprocessing + H2D + first forward | Yes |
| `mean_tpot_ms` | Mean of the `output_tokens - 1` decode intervals | No |
| `p95_itl_ms` | Nearest-rank P95 of those decode intervals | No |
| `generation_ms` | First forward + all post-first-token decode intervals | No |
| `e2e_latency_ms` | Preprocessing + H2D + generation | Yes |
| `output_tokens_per_second` | All batch output tokens / generation seconds | No |
| `requests_per_second` | Static batch size / generation seconds | No |

Latency is one static batch's completion time. It is not a distribution of online
request latency, and aggregate output throughput is not divided by batch size and
relabeled as per-request throughput.

## Workloads

All workloads use cache-on greedy decoding, seed 42, two warmups, and five measured
attempts per point. The 12 workload points yield exactly 60 terminal raw rows.
Before these attempts, the harness must pass shape smoke for batch 1, 2, and 4 and
must prove that batch=1 produces exactly the same token IDs as the single-request loop.

| Sweep | Changed variable | Fixed variables | Workload points |
|---|---|---|---|
| Prompt length | 32, 256, 1024, 2048 prompt tokens | batch=1, output=64 | 4 |
| Output length | 16, 64, 256 output tokens | batch=1, prompt=256 | 3 |
| Static batch | batch=1, 2, 4, 8, 16 | prompt=256, output=64 | 5 |

Row zero uses the configured prompt unchanged so batch=1 has an exact parity reference.
For larger batches, only rows 1 onward receive a synthetic request number before all
rows are normalized to the controlled token length.

## Results

<!-- WEEK02: replace table with measured median/P95 values and explain all OOM/error rows -->

| Sweep point | Completed / OOM / error | Median primary latency | Median throughput |
|---|---:|---:|---:|
| Pending measured summary | pending | pending | pending |

![Prompt length versus TTFT](../results/week02/figures/prompt-length-vs-ttft.png)

![Output length versus E2E latency](../results/week02/figures/output-length-vs-e2e-latency.png)

![Batch size versus token throughput](../results/week02/figures/batch-size-vs-token-throughput.png)

![Batch size versus latency and memory](../results/week02/figures/batch-size-vs-latency-memory.png)

<!-- WEEK02: write at least three numerical conclusions with units from this run -->

## Memory Model

The theoretical cache tensor size is:

```text
2 × layers × KV heads × head dimension × sequence length × batch × bytes/element
```

It uses `num_key_value_heads`, not all attention heads. It excludes model weights,
activations, allocator fragmentation/cache, CUDA context, and framework workspaces.
The observed peak cache length is `prompt_tokens + output_tokens - 1`: the first output
token comes from prefill, so only the remaining decode forwards extend the cache.

<!-- WEEK02: compare theoretical KV MiB with measured allocated/reserved deltas -->

## Interpretation

<!-- WEEK02: answer linearity, TPOT stability, marginal throughput, and latency source -->

Do not infer a result from the expected shape of the curves. Explain only patterns
present in this run's raw repeats, including variance and capacity boundaries.

## Limitations

- This is eager Hugging Face inference, not a production serving engine.
- It measures homogeneous static batches, not arrival processes or continuous batching.
- Every request generates a fixed token count; EOS does not produce variable completion.
- Results from one L4 and this small model do not generalize directly to other hardware.
- Synthetic fixed-length prompts control shape but are not a representative prompt corpus.
- Official completion accepts an OOM point only when all five measured attempts OOM and
  it lies in a monotonic suffix of that sweep; mixed points and ordinary errors fail.

## Next Steps

- Use this static-batch baseline to separate compute gains from Week 3 queueing effects.
- Add dynamic arrivals, batch windows, queue delay, and online tail-latency accounting.
