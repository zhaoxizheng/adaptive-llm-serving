# Week 10 Plan: GPU Worker、Model Runner 与执行边界

> 时间预算：10–12 小时
>
> 本周主线：完成 vLLM 核心请求链路。把 Week 8 的 scheduler output 和 Week 9 的 KV block table 继续追踪到 GPU Worker、Model Runner、model forward、sampling 与 output copy，解释一次 engine step 如何变成一次真实 GPU execution。

## 本周目标

完成本周后，应当能够：

1. 解释 GPU Worker 与 GPU Model Runner 的职责边界和生命周期。
2. 追踪模型加载、显存 profiling、KV cache 初始化和 warmup/capture。
3. 解释 scheduler output 如何更新 persistent batch，并形成 model input。
4. 区分 prefill、decode 与 mixed step 在 input shape 和执行路径上的差异。
5. 识别 eager execution 与 CUDA Graph replay 的选择条件。
6. 从 engine step 追踪到 logits、sampling 和 engine output。

## 本周边界

- 继续使用 Week 7 固定 revision 和 Week 6 的单卡 operating point。
- 只追踪当前模型实际使用的 attention backend、sampling path 和 executor。
- 本周理解调用边界与数据变换，不深入单个 CUDA kernel；kernel 分析留到 Week 13。
- CUDA Graph 只验证 dispatch/capture/replay，不做完整优化结论。
- 不同时研究 tensor parallel、pipeline parallel、speculative decoding 或 multimodal。
- Trace 不复制 tensor 数据，不记录 logits、token 内容或 prompt。

## 本周最终产出

- `docs/vllm-worker-model-runner-map.md`：worker 初始化与每步执行地图
- `docs/vllm-step-shapes.md`：prefill、decode、mixed step 的 shape ledger
- `patches/week10-execution-trace.patch`：CPU boundary 与 NVTX range patch
- `scripts/run_week10_execution_scenarios.sh`：eager/graph 和三种 step 场景
- `results/week10/traces/`：engine、CPU 与 GPU 对齐事件
- `reports/week10.md`：完整 API-to-GPU 请求链路与阶段验收

## 每步执行模型

```text
SchedulerOutput
    ↓ update request / block-table state
persistent input batch
    ↓ prepare positions, slots, attention metadata
GPU tensors
    ↓ eager launch or CUDA Graph replay
model forward
    ↓ logits / sampling
sampled token IDs
    ↓ copy / detokenize in upper layer
EngineCoreOutput
```

对每个箭头同时记录：执行进程、关键类/方法、输入输出 shape、是否发生 CPU↔GPU copy、是否同步。

## Shape Ledger

每个实验 step 至少保存以下摘要：

| 字段 | 说明 |
|---|---|
| `step` | 与 Week 8 scheduler trace 对齐的 step ID |
| `request_count` | 本步 active requests |
| `scheduled_tokens` | 本步实际执行 token 总数 |
| `prefill_tokens` / `decode_tokens` | workload composition |
| `input_ids_shape` | 模型输入的逻辑 shape |
| `positions_shape` | position 输入 shape |
| `kv_slot_count` | 本步写入 KV 的 slots |
| `execution_mode` | eager、graph capture 或 graph replay |
| `cpu_prepare_us` / `gpu_execute_us` | 仅用于机制对比的区间 |

shape ledger 只保存维度与计数，不保存 tensor 内容。

## 四个执行场景

### Scenario A：Single-request prefill

- 固定短 prompt，限制 output 为 1 token。
- 目标：找到输入准备、attention、forward、sampling 和 output 的完整边界。

### Scenario B：Steady decode

- 单请求生成足够多 token，忽略第一个 prefill step。
- 目标：观察 decode shape、persistent batch update 和每步 execution mode。

### Scenario C：Mixed step

- 在一个请求 decode 时加入新的长 prompt。
- 目标：确认当前版本如何把 decode 与 chunked prefill 组合成 model input。

### Scenario D：Eager vs CUDA Graph

- 在当前版本允许的配置下，对相同短 decode workload 分别运行 enforce-eager 与 graph path。
- 目标：证明 graph 是否被 replay，并识别 CPU launch gap；本周不以少量样本宣布吞吐收益。

## 每日安排

### Day 1：Worker 初始化与显存生命周期（约 1.5–2 小时）

- [ ] 从 Engine Core executor 进入 GPU Worker
- [ ] 追踪 device init、model load 和 distributed init 的实际顺序
- [ ] 找到 memory profiling 与 KV cache capacity 的连接点
- [ ] 找到 warmup、dummy run 和 graph capture 入口
- [ ] 为关键方法建立固定 commit permalink

### Day 2：Scheduler output 到 input batch（约 1.5–2 小时）

- [ ] 追踪新增、继续、完成请求如何更新 batch
- [ ] 找到 token IDs、positions 和 block table 的准备位置
- [ ] 记录 CPU-side state 与 GPU-side tensor 的边界
- [ ] 解释 persistent batch 如何避免每步完全重建
- [ ] 完成 Scenario A 的手工 shape 预测

### Day 3：Model forward 与 attention backend（约 1.5 小时）

- [ ] 找到实际 model callable 和 forward invocation
- [ ] 识别当前模型选择的 attention backend
- [ ] 区分 prefill、decode 与 mixed metadata
- [ ] 追踪 KV slot mapping 如何进入 attention layer
- [ ] 不进入不在当前路径上的 backend 实现

### Day 4：Sampling 与 output（约 1.5 小时）

- [ ] 追踪 hidden states 到 logits
- [ ] 找到 sampling params 如何影响 sampler
- [ ] 追踪 sampled IDs 返回 Engine Core 的路径
- [ ] 标记 CPU↔GPU copy 与可能的 synchronization
- [ ] 与 Week 7 output trace 对齐

### Day 5：Execution trace 与 shape ledger（约 2 小时）

- [ ] 在 scheduler、prepare、forward、sample、output 添加边界事件
- [ ] 使用 NVTX range 标注 GPU timeline
- [ ] 运行 Scenario A/B/C
- [ ] 自动检查 scheduled token 与 input shape 是否一致
- [ ] 确认 trace 默认关闭且不暴露 tensor 内容

### Day 6：Eager 与 CUDA Graph（约 1.5–2 个计费小时）

- [ ] 记录当前 graph capture sizes 与选择条件
- [ ] 分别运行 eager 和 graph replay 场景
- [ ] 证明实际走到哪条路径，而不是只看启动参数
- [ ] 观察 replay 前后的 CPU launch pattern
- [ ] 同步 trace 后停止 VM

### Day 7：完成核心链路验收（约 1.5 小时）

- [ ] 将 Week 7–10 source maps 合并为一张端到端图
- [ ] 标出 API、IPC、scheduler、KV、worker 与 GPU 边界
- [ ] 为每个箭头提供 source permalink 或 runtime event
- [ ] 列出 Week 11 profiler 要验证的三个瓶颈假设
- [ ] 记录多卡与 kernel-level 未覆盖分支

## 报告必须回答的问题

1. Worker 初始化时，model weights 与 KV cache 分别何时占用显存？
2. Scheduler output 如何改变 persistent batch？
3. Prefill、decode 与 mixed step 的 input shape 有何不同？
4. Block table 与 slot mapping 如何进入 attention 执行？
5. 当前 workload 为什么选择 eager 或某个 CUDA Graph？
6. Sampling 在哪个进程和 device 上完成，结果何时返回 Engine Core？
7. 一次 client-visible token 横跨了哪些 CPU、IPC 与 GPU 边界？

## 本周不要做什么

- 不按 worker 目录逐文件阅读。
- 不把 tensor 内容、logits 或 token IDs 写入 trace。
- 不把 CUDA API duration 当作 kernel duration。
- 不根据配置名判断 graph 已启用，必须观察 capture/replay 证据。
- 不在本周同时加入 tensor parallel 或 speculative decoding。
- 不用 debug instrumentation run 更新 Week 6 的正式 operating point。

## 完成标准

- [ ] Worker、Model Runner 与 model forward 入口已绑定固定 revision
- [ ] 四个场景都有 shape ledger 和边界 trace
- [ ] Scheduler scheduled tokens 与 Model Runner 输入能够对齐
- [ ] Prefill、decode 和 mixed step 的差异可以用实际 shape 解释
- [ ] Eager/graph path 有 runtime 证据
- [ ] Sampling 与 output return 路径已追踪完成
- [ ] Week 7–10 端到端请求链路图完成
- [ ] Week 11 的 profiling hypotheses 已写成可证伪问题
- [ ] GCP VM 已停止，结果已同步并绑定 Git commit
