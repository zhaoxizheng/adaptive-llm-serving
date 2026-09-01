# Week 6 Plan: 量化、参数调优与可部署 Operating Point

> 时间预算：10–12 小时
>
> 本周主线：在 Week 5 的观测与 SLO contract 上，完成一个受控的 vLLM 单实例调优实验。先比较 BF16 与一种当前环境明确支持的量化方案，再围绕显存容量和 scheduler 限制做小矩阵参数 sweep，选出可部署 operating point。

## 本周目标

完成本周后，应当能够：

1. 区分权重量化、activation 量化和 KV cache dtype，它们不是同一个变量。
2. 解释量化为什么减少模型权重显存，却不保证所有 workload 的 latency 都下降。
3. 用 Week 5 的 SLO goodput 判断配置收益，而不是只比较模型能否加载。
4. 分别解释 `gpu-memory-utilization`、`max-num-seqs` 和 `max-num-batched-tokens` 的约束。
5. 使用单变量和小型二阶段 sweep，避免从不可解释的大网格中挑最好数字。
6. 完成 vLLM 用户阶段验收：给定 workload 和 SLO，能够选择并复现合理配置。

## 本周边界

- 正式对比固定同一 GPU、模型家族、tokenizer、workload、sampling 和 vLLM 版本。
- 强制完成 BF16 baseline 和一个受支持的 weight-only quantization；第二种量化方案是可选项。
- 使用发布者提供并记录 revision 的量化 checkpoint，不在本周实现量化算法或自行校准大模型。
- 不将不同参数量模型的结果写成“量化前后”对比。
- 质量评估只做生成一致性和小型任务 sanity check；完整模型质量 benchmark 不在本周范围。
- Week 7 开始源码阅读，因此本周不修改 scheduler。

## 本周最终产出

- `configs/week06.yaml`：模型 variant、server args、load points 和 SLO
- `scripts/start_vllm_variant.sh`：按 variant 启动并保存解析后的配置
- `scripts/benchmark_week06.sh`：运行 smoke、quality sanity 和 performance matrix
- `src/validate_outputs.py`：固定 prompts 的输出与错误检查
- `src/analyze_week06.py`：显存、goodput、latency 和 cost comparison
- `results/week06/raw/`：每个 variant 的版本、日志、client/server/GPU 数据
- `results/week06/figures/`：quantization 与参数敏感性图
- `reports/week06.md`：最终 operating point、适用范围和回退配置

## 实验变量

### 模型表示

最低实验集：

1. `bf16` 或当前 L4 环境实际支持的未量化 baseline。
2. 一个 vLLM support matrix 明确支持、且有可信预量化 checkpoint 的方案，例如 AWQ 或 GPTQ。

可选实验：第二个 weight-only 方案或 FP8。只有在当前 NVIDIA L4、CUDA、vLLM 和 checkpoint 组合被明确支持时才运行。不要为了填表强行比较无法公平加载的格式。

每个 variant 记录：

- model ID、revision、quantization method 和 config
- tokenizer ID、revision 和 chat template
- weight dtype、compute dtype 和 KV cache dtype
- model load 后 GPU memory footprint
- engine startup time 与任何 fallback/warning

### Engine 参数

先固定模型表示，以 Week 5 baseline 为中心做小矩阵：

```yaml
gpu_memory_utilization: [0.80, 0.90]
max_num_seqs: [8, 16, 32]
max_num_batched_tokens: [2048, 4096, 8192]
```

这些只是候选值。执行前根据模型大小、`max-model-len` 和启动日志缩小范围；若某个值无法加载，记录为 capacity failure，不反复盲试。

## 两阶段实验设计

### 阶段 A：量化表示对比

固定 Week 5 server 参数与三个 load point：

- low：远低于 capacity，用于观察单请求 overhead
- boundary：BF16 接近 SLO 边界的稳定点
- overload：BF16 第一个不稳定点，用于观察量化是否扩展 capacity

对 short chat、long context 和 generation workload 比较：

- load success 与 startup warning
- static GPU memory 与可用 KV cache capacity
- P99 TTFT、TPOT 和 E2E
- request/output token throughput
- SLO goodput
- output validation result

### 阶段 B：Engine 参数调优

只选择阶段 A 中综合最合理的一个 model variant：

1. 先 sweep `max-num-seqs`，定位并发限制是否过紧或过松。
2. 固定较优值，再 sweep `max-num-batched-tokens`。
3. 只在 KV cache capacity 明显不足时比较 `gpu-memory-utilization`。
4. 对候选最优配置做 3 次完整重复和一次较长 soak run。

## 质量与正确性 Sanity Check

固定 20–50 条小型 prompt，覆盖：

- 短问答
- 长上下文信息抽取
- 简单算术或结构化输出
- 中英文输入
- 接近 `max-model-len` 的边界输入

使用 greedy decoding 或固定 seed。检查：

- HTTP 与 schema 正确
- 无 NaN、空输出、乱码或异常重复
- 实际 input/output token 数符合 contract
- 结构化任务的 exact check
- 人工抽查量化前后明显退化

通过 sanity check 只表示该 variant 可以进入性能实验，不代表量化质量与 baseline 等价。

## Operating Point 选择规则

候选配置必须：

1. 所有 smoke 和 output validation 通过。
2. 在目标 workload mixture 下 error rate 为 0 或低于预设阈值。
3. 满足 Week 5 固定的 TTFT、TPOT 和 E2E SLO。
4. Goodput 相对 baseline 有可重复收益，或在近似性能下明显降低显存。
5. 具有至少 10% 的 KV cache 或负载余量，不贴着 OOM 边界运行。
6. 启动参数、模型 revision 和环境可以一条命令重建。

同时保存保守回退配置；如果量化 checkpoint 不可用或 quality sanity 失败，BF16 tuned configuration 就是有效结论。

## 每日安排

### Day 1：Support matrix 与实验 contract（约 1.5 小时）

- [ ] 固定 GPU、driver、CUDA 和 vLLM 版本
- [ ] 查当前版本 quantization support matrix
- [ ] 选择 BF16 与一个可验证的预量化 checkpoint
- [ ] 记录 checkpoint revision、license 和 tokenizer compatibility
- [ ] 固化 Week 5 三个 load point 与 SLO，不重新挑有利阈值

### Day 2：加载、显存与正确性（约 1.5–2 个计费小时）

- [ ] 分别启动各 model variant
- [ ] 保存 startup log、resolved engine args 和 model config
- [ ] 测量 idle/model memory 与可用 KV cache
- [ ] 运行相同 smoke 和 output validation
- [ ] 将不支持、OOM 和质量异常作为结果保存

### Day 3：量化低负载与边界负载（约 2 个计费小时）

- [ ] 对 short chat、long context、generation 运行 low load
- [ ] 在 BF16 boundary load 比较各 variant
- [ ] 每个有效 case 至少 3 次重复
- [ ] 同步 client、Prometheus、GPU 与 log evidence

### Day 4：Capacity 与 Goodput（约 1.5–2 个计费小时）

- [ ] 运行 overload point，并在新拐点附近补少量 load point
- [ ] 比较量化是否提升最大稳定 request rate
- [ ] 检查 memory saving 是否转化为更高 KV capacity
- [ ] 区分 TTFT、TPOT 与吞吐的不同变化

### Day 5：Engine 参数小矩阵（约 2 个计费小时）

- [ ] 依次 sweep `max-num-seqs` 和 `max-num-batched-tokens`
- [ ] 仅在必要时 sweep `gpu-memory-utilization`
- [ ] 每次只改变一个主变量
- [ ] 在 mixed workload 下验证参数收益没有依赖单一 shape

### Day 6：Soak、边界和回退（约 1–1.5 个计费小时）

- [ ] 对候选配置做 20–30 分钟 soak run
- [ ] 检查 memory growth、queue drift、error 和 preemption warning
- [ ] 测试接近最大 context 的请求
- [ ] 验证保守回退配置可启动并满足最低 SLO
- [ ] 同步结果后停止 VM

### Day 7：报告与阶段验收（约 1.5–2 小时）

至少生成：

1. `variant-vs-model-memory.png`
2. `variant-vs-goodput.png`
3. `variant-vs-p99-ttft-tpot.png`
4. `max-num-seqs-sensitivity.png`
5. `max-num-batched-tokens-sensitivity.png`

报告必须同时写出最终配置、回退配置、不适用的 workload 和仍未完成的质量验证。

## 本周不要做什么

- 不比较不同参数量模型后声称是量化收益。
- 不把“模型成功加载”当作性能或质量结论。
- 不同时改变 quantization、KV dtype、max model length 和 scheduler 参数。
- 不因某个 variant OOM 而静默缩短其 context length。
- 不只报告 average throughput，遗漏 P99、error 和 goodput。
- 不对 20 条 sanity prompt 的结果做完整质量 benchmark 声明。

## 完成标准

- [ ] BF16 与至少一个受支持量化 variant 完成受控比较
- [ ] 各 variant 的 model、tokenizer、dtype 和 revision 完整记录
- [ ] 输出 sanity check 通过，或明确记录失败并停止性能结论
- [ ] 至少完成 `max-num-seqs` 和 `max-num-batched-tokens` 小矩阵
- [ ] 最终 operating point 在目标 mixture 下满足固定 SLO
- [ ] 候选配置通过重复实验和 soak run
- [ ] 回退配置已验证
- [ ] 第二阶段验收完成：能根据 SLO 选择单实例配置
- [ ] GCP VM 已停止，结果已同步并绑定 Git commit
