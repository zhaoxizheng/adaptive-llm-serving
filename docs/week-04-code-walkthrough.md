# Week 4 Code Walkthrough: vLLM as a Versioned Service

> This describes the implemented workflow. It does not claim that an L4 benchmark
> has run. GPU compatibility remains unverified until generation smoke succeeds.

## 1. Dependency boundary

Week 1–3 dependencies stay in `.venv`; Week 4 uses `.venv-vllm`.
`requirements-vllm.txt` pins `vllm==0.10.2`. The bootstrap archives pre-install
GPU/Python information, the resolved package set, the vLLM version, and three CLI
help surfaces. Installed help is the authority for all flags used at execution time.

## 2. Experiment contract and one-run identity

`configs/week04.yaml` separates scientific inputs from output paths.
`src.vllm_contract` validates the immutable model revision, localhost-safe server
settings, workload shapes, repetitions, sweeps, and SLOs. Its fingerprint excludes
artifact locations. `expand_cases()` gives every closed/open-loop repeat a stable,
unique ID; `server_argv()` and `benchmark_argv()` produce argv arrays rather than
shell command strings.

An official run creates `run-metadata.json` before any GPU work. Its UUID is the
logical experiment identifier used by the environment snapshot, server ownership
record, smoke tests, benchmark sidecars, normalized records, comparison replay,
analysis, report, audit, and verification receipt. Each actual HTTP server process
start also gets a fresh `server_attempt_id`; readiness, both HTTP smokes, benchmark
smoke, every formal case and sidecar/completion marker, trace replays, their manifest,
analysis, and the verification receipt all bind to that exact attempt. The metadata
also freezes the scientific config, complete
critical source inventory (including `requirements-vllm.txt`), runtime/GPU identity,
and model identity. Resume is allowed only when those identities still match.

Useful local checks require no vLLM installation:

```bash
python -m src.vllm_contract validate --config configs/week04.yaml
python -m src.vllm_contract cases --config configs/week04.yaml
python scripts/start_vllm.py --config configs/week04.yaml plan
bash scripts/benchmark_vllm.sh --config configs/week04.yaml \
  --case-id <case-id> --plan
bash scripts/run_week04.sh --plan
```

Plan output says capability preflight is pending. It is not runtime evidence.
The operational phases are deliberately separate:

```bash
# On the GPU VM: initialize one UUID and collect evidence.
bash scripts/run_week04.sh --gpu

# After syncing results and stopping the VM: run locally/offline.
bash scripts/run_week04.sh --analyze
bash scripts/run_week04.sh --audit-template
# Fill the audit with real project/zone/instance/commands after checking GCP.
bash scripts/run_week04.sh --verify
```

The GPU phase cannot claim that its own VM is stopped. The audit template starts
unconfirmed, and final verification rejects it until external stop and residual
resource checks are recorded.

## 3. Offline and HTTP smoke paths

`src.vllm_offline_smoke` imports vLLM only inside the execution function, checks the
installed version, and calls `LLM.generate` with a pinned revision and deterministic
sampling. `src.openai_smoke` sends both ordinary JSON and streaming completions.
The shared `src.openai_stream` implementation uses only the Python standard library,
so importing or testing it does not require vLLM, Torch, or the OpenAI SDK.

The SSE parser uses an incremental UTF-8 decoder and accepts fragmented CR, LF,
CRLF, multi-line data, comments, metadata, and `[DONE]`. It bounds lines, events,
HTTP bodies, socket operations, and total stream duration and sanitizes errors.

## 4. Server safety and lifecycle

`scripts/start_vllm.py` defaults to `127.0.0.1`. A non-loopback bind requires the
explicit `--allow-non-loopback` acknowledgement. Start captures the installed
version/help and rejects any requested flag absent from that captured help.
Readiness is bounded and requires both `/health` and an expected model from
`/v1/models`.

The process record contains the run UUID, a logical server-instance UUID, a fresh
per-process server-attempt UUID, PID, process-group identity, OS process-start identity,
and exact observed argv. The readiness snapshot wraps the raw `/v1/models` response
with the same run, instance, and attempt identity. Stop sends signals only
while ownership matches, targets the owned process group, and waits for both group
exit and port closure. This prevents stale PID reuse and orphaned engine workers.

A failed logical run may start another process only if the previous attempt produced
no completed server-backed evidence. Once readiness, an HTTP smoke, a completed
benchmark (including benchmark smoke), or a replay artifact exists, restart is refused;
archive that run and initialize a new run UUID. This prevents a resume from silently
combining artifacts created by different server processes.

## 5. Official benchmark evidence

`scripts/benchmark_vllm.sh` wraps `vllm bench serve` without parsing terminal text.
It archives the exact argv and installed benchmark help, checks every option against
that help, and asks vLLM to write detailed machine-readable JSON. It first writes an
`incomplete` marker and a candidate file in a temporary directory. Only exit code 0
plus syntactically valid JSON publishes the final raw artifact. Failed and interrupted
runs keep their marker/stdout and cannot be confused with completed cases.
`benchmark.timeout_seconds` is enforced by a monotonic process-group watchdog. Timeout
sends TERM, waits a bounded grace period, escalates to KILL, and retains structured
watchdog evidence. A completion marker hashes the raw JSON, exact argv, stdout, CLI
help, watchdog record, and before/after metrics.

## 6. Exact trace replay, normalization, and figures

The random vLLM matrix and Week 3 comparison are separate evidence. For comparison,
`src.openai_trace_client` first runs the read-only Week 3 verifier and requires its
completed receipt/status to match the freshly reconstructed official 55-case
primary/HF evidence. It then validates and replays persisted Week 3 trace CSVs. It
preserves trace/request IDs, relative arrival offsets, the warmup boundary, and
requested 256/64 token shapes, and records actual start lag so client backpressure
cannot masquerade as open-loop load. `comparison.base_prompt` must exactly equal the
Week 3 workload prompt, and both backends use the shared request-ID-derived token
constructor, so the prompt-token sequence is identical as well.

`src.vllm_result_adapter` converts the pinned 0.10.2 aggregate into a repository-owned
schema while binding it to the raw-file SHA-256. Requested token targets stay separate
from actual per-request token counts. Success, timeout, error, latency, throughput,
and queue metrics are validated rather than silently defaulted.

`src.analyze_week04` generates four figures plus a machine-readable analysis. Its
Week 3 comparison gate accepts only balanced open-loop records with matching trace IDs,
request rates, model, revision, and dtype. Closed-loop concurrency is never placed on
the same x-axis as request rate. Server queue time remains distinct from client TTFT.

## 7. Evidence gate

`scripts/verify_week04.py` is reconstructive and offline. It requires an exact raw case
set, rejects every incomplete marker, re-hashes all sidecars, re-normalizes each raw
result, and requires exact normalized records. It also checks the single run/source/
config/runtime/model identity and one exact server-attempt identity across readiness,
both HTTP smokes, benchmark smoke, every formal case, and every replay artifact, plus
the trace comparison, figures, and machine-readable operating point. The comparison
manifest hashes the Week 3 config, metadata, aggregate
events, completed verification receipt, and completed run status, so final verification
cannot silently switch to a partial or different source run. The report must cite the
run UUID and measured request rate, request
and token throughput, P99 TTFT/TPOT, queue time, all outcome counts/error rate, and full
server arguments. Configured SLO text alone is not evidence. Verification finally
requires a concrete identity-bound stop/resource audit before promoting the status.
