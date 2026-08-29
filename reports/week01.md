# Week 1: Prefill, Decode, and KV Cache

## Question

How do prompt length, output length, and KV caching affect single-request LLM inference latency and throughput?

## Environment

Complete after running `make check-env`. Link or summarize `results/week01/environment.json`.

## Method

- Model: `Qwen/Qwen2.5-0.5B-Instruct`
- Decoding: greedy
- Warmup: 2 runs
- Repetitions: 5 per configuration
- Prompt tokens: 32, 256, 1024
- Output tokens: 32, 128
- Independent variable: KV cache on/off

## Results

Add the two generated figures and a compact table of median results.

## Observations

Record observations only after reviewing the raw data. Include numbers and workload conditions.

## Limitations

- This is single-request eager Hugging Face inference, not production serving.
- The model is intentionally small and does not represent large-model memory pressure.
- Results from one GPU type should not be compared directly with another GPU type.
- The experiment does not cover continuous batching, request scheduling, or network latency.

## What I Learned

Explain prefill, decode, KV cache, TTFT, and TPOT in your own words.

## Next Week

- Add systematic sequence-length sweeps.
- Measure batch-size effects.
- Break down model weights, activations, and KV-cache memory.

