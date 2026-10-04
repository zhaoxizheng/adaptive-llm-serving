# Engine step 的 shape ledger

实现见 [execution helper](../scripts/execution_trace_payload.py) 的 `shape()` 和
[parser](../src/parse_execution_trace.py)。本实验限定 text-only、普通 positions、单 GPU。

| 字段 | 来源与解释 |
|---|---|
| `step` | scheduler 产生并通过 `SchedulerOutput.study_step` 传入 runner |
| `request_count` | 本步 `num_scheduled_tokens` 中的 request 数 |
| `scheduled_tokens` | 本步真实调度 token 总数 |
| `prefill_tokens` | 每请求 `min(scheduled, max(0, prompt-computed))` 后求和 |
| `decode_tokens` | `scheduled_tokens - prefill_tokens`，当前范围无 spec tokens |
| `input_ids_shape` / `positions_shape` | 只读取实际 tensor 的 shape，text path 为一维 |
| `padded_tokens` | runner preprocess 后的 model input 长度，可因 graph padding 大于真实 tokens |
| `kv_slot_count` | 本支持范围内有效 KV 写入 token 数；排除 graph padding 的无效 slots |
| `dispatch_mode` | dispatcher 的枚举，表示选择意图 |
| `execution_mode` | 根据真实 capture/replay event 判定，证据不足为 `unproven_graph` |
| `cpu_prepare_us` | update_batch、prepare、preprocess 三个 CPU range 的总长 |
| `gpu_execute_us` | Week 10 留空；需从 profiler 的 CUDA event 另行测量 |

例如逻辑 token 数为 5、捕获图接受 8 个 token 时：

```text
scheduled_tokens = 5
input_ids_shape = positions_shape = [8]
padded_tokens = 8
kv_slot_count = 5
```

不能写成 `input_ids_shape[0] == scheduled_tokens`，否则会错误拒绝合法 graph padding。
Parser 检查 shape 等于实际 padded count，且 padded count 不小于逻辑 token 数。

单步可以同时包含未完成的 prompt 和 decode。`composition=mixed` 来自实际 token
进度，不来自 workload 名称。到达间隔为 100ms 的 mixed 场景不保证每次运行都产生
mixed step；未观察到时保留 `not_observed`，调整一个变量后另建 session。
