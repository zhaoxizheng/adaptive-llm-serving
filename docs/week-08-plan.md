# Week 8 Plan: Scheduler、Token Budget 与 Chunked Prefill

> 时间预算：10–12 小时
>
> 本周主线：在 Week 7 请求链路上深入 Engine Core scheduler。围绕“每一步选择哪些请求、分配多少 token、为什么等待或抢占”阅读代码，并用低开销结构化 trace 重建 request state timeline。

## 本周目标

完成本周后，应当能够：

1. 解释 WAITING、RUNNING、PREEMPTED/REQUEUED 和 FINISHED 等请求状态如何变化。
2. 解释 `max-num-seqs`、`max-num-batched-tokens` 与可用 KV cache blocks 如何共同限制一步调度。
3. 说明 decode、prefill 和 chunked prefill 在 token budget 中如何竞争。
4. 人为构造 token-budget pressure 和 KV-cache pressure，并区分二者证据。
5. 从 scheduler trace 重建每一步 scheduled requests、scheduled tokens 和 queue depth。
6. 比较 scheduler trace 与 Week 5 的外部 queue/TTFT 指标，连接内部机制与用户体验。

## 本周边界

- 继续使用 Week 7 固定的 vLLM revision、单 GPU 和小模型。
- 主实验聚焦 V1 scheduler；旧版本或其他 scheduler policy 只做差异备注。
- 本周观察 KV block allocation 的调用与结果，但 KV cache manager 算法、prefix cache 和 eviction 深入分析留到 Week 9。
- 不修改 scheduling policy，不把 instrumentation overhead 混入正式 Week 5/6 性能数字。
- Trace 只运行短、小、确定性的 workload，避免产生不可分析的大日志。

## 本周最终产出

- `docs/vllm-scheduler-map.md`：scheduler 入口、状态、预算和 KV 交互
- `patches/week08-scheduler-trace.patch`：默认关闭的结构化 trace
- `scripts/run_week08_scenarios.sh`：四个确定性调度场景
- `src/parse_scheduler_trace.py`：验证事件并生成 request/step 表
- `results/week08/traces/`：baseline、mixed、token pressure、KV pressure
- `results/week08/figures/`：step timeline、queue、token budget 和 preemption 图
- `reports/week08.md`：逐问题解释 scheduler decision

## Scheduler Decision Model

每一个 scheduler step 都尝试回答：

```text
Given:
  waiting requests
  running requests
  token budget
  sequence/concurrency limit
  available KV cache blocks
  request priorities and states

Decide:
  which requests run this step
  how many tokens each request schedules
  which requests remain waiting
  whether any request is preempted/requeued
  whether the step is empty or finished
```

先从固定 revision 的源码提取真实规则，再用这张模型校验；不要把概念模型当成实现事实。

## Trace Schema

每个 step 至少记录：

```json
{
  "step": 12,
  "ts_ns": 0,
  "running_before": 3,
  "waiting_before": 2,
  "token_budget_initial": 256,
  "scheduled": [{"request_id": "r1", "tokens": 1}],
  "preempted": [],
  "running_after": 3,
  "waiting_after": 2,
  "token_budget_remaining": 253
}
```

若源码已有 structured logging 或 debug dump，优先复用。自定义 patch 不复制 scheduler 算法，只在 decision 已产生的位置记录结果。

Trace parser 检查以下 invariant：

- `scheduled_tokens <= initial_token_budget`
- scheduled request 数不超过配置允许的 active sequences
- request state transition 合法
- finished request 不再出现在后续 scheduled set
- preempted/requeued request 后续要么再次运行，要么明确 abort/error

## 四个确定性场景

### Scenario A：单请求 baseline

- prompt 32，output 8
- 目标：识别 prefill step、decode steps 和 finish
- 回答：一次请求在 trace 中最小生命周期是什么？

### Scenario B：Mixed prompt / output

- 一个 long-prefill request，加两个 short requests
- 固定到达顺序与间隔
- 目标：观察 short request 何时进入 running，以及 decode slot 如何复用

### Scenario C：Token-budget pressure

- 降低 `max-num-batched-tokens`
- 使用超过单步 budget 的长 prompt
- 目标：观察 chunked prefill 与 decode token 如何共享 budget

### Scenario D：KV-cache pressure

- 降低可用 KV capacity 或增加足够长的并发请求
- 保持 token budget 足够，减少与 Scenario C 的混淆
- 目标：触发 allocation failure、preemption 或 requeue，并记录准确原因

如果小模型和 L4 无法稳定触发 KV pressure，先用更长 context 或较低 `gpu-memory-utilization`，但不改模型与多个参数同时追结果。

## Chunked Prefill 对比

只在当前版本支持且行为明确时，对同一 long-prefill + decode workload 比较：

- chunked prefill enabled
- chunked prefill disabled（若当前版本允许）

观察：

- long request TTFT
- 已在 running 的 decode request TPOT/ITL
- 每步 scheduled prefill tokens
- queue wait 和总完成时间

这不是完整性能调优；目标是用 trace 解释机制和 trade-off。

## 每日安排

### Day 1：Scheduler source map（约 1.5–2 小时）

- [ ] 从 Week 7 Engine Core 调用点进入 scheduler
- [ ] 找到 scheduler 初始化、add request、schedule 和 update/finish 路径
- [ ] 列出 request state enum 和核心 collections
- [ ] 找到 token budget 与 active sequence limit 的来源
- [ ] 为所有关键函数创建固定 commit permalink

### Day 2：Decode 与 prefill decision（约 1.5 小时）

- [ ] 跟踪 running requests 如何获得 scheduled tokens
- [ ] 跟踪 waiting requests 如何被 admit
- [ ] 找到 long prompt 被 chunk 的条件
- [ ] 记录 LoRA、encoder input 等非主线分支，但不深入
- [ ] 用手算预测 Scenario A/B 的前几个 step

### Day 3：KV allocation 与 preemption 入口（约 1.5–2 小时）

- [ ] 找到 scheduler 请求 KV blocks 的调用点
- [ ] 区分 token budget 不足与 KV allocation 不足
- [ ] 追踪 preempt/requeue 的 state update
- [ ] 找到 finished/aborted request 的释放调用
- [ ] 将 KV manager 的未解算法问题移交 Week 9

### Day 4：实现最小 scheduler trace（约 2 小时）

- [ ] 在 decision 生成后记录 step summary
- [ ] 加入 request state transition event
- [ ] 使用环境变量控制并写到独立 JSONL
- [ ] 实现 parser 和 invariant validation
- [ ] 运行 Scenario A，校正 trace 字段

### Day 5：Mixed 与 token pressure（约 1.5–2 个计费小时）

- [ ] 运行 Scenario B 并绘制 request-step timeline
- [ ] 运行 Scenario C，确认长 prefill 被分成多个 step
- [ ] 对照 client TTFT/TPOT 与 internal scheduled steps
- [ ] 若比较 chunked prefill off/on，每个配置只运行短场景

### Day 6：KV pressure 与 preemption（约 1.5–2 个计费小时）

- [ ] 运行 Scenario D 并确认压力确实来自 KV capacity
- [ ] 捕获 allocation、preemption/requeue 和恢复事件
- [ ] 验证 request 最终完成或明确失败
- [ ] 检查释放后 KV 使用是否回落
- [ ] 同步 trace 并停止 VM

### Day 7：关联外部指标与报告（约 1.5–2 小时）

至少生成：

1. `request-state-timeline.png`
2. `step-scheduled-tokens.png`
3. `step-running-waiting.png`
4. `token-vs-kv-pressure.png`
5. `internal-queue-vs-client-ttft.png`

报告对每个 scenario 写出：配置 → 预期 → trace 证据 → client 影响 → 结论。

## 本周不要做什么

- 不从函数名猜测 scheduler policy，必须读条件与数据结构。
- 不在 inner loop 打印每个 token 的完整对象。
- 不将 token budget exhaustion 与 KV cache exhaustion 混为一谈。
- 不通过同时降低多个限制来“保证触发” preemption。
- 不用 instrumentation run 的吞吐与 Week 5 无 trace baseline 直接比较。
- 不在本周提出新的 scheduler policy 并宣称优化。

## 完成标准

- [ ] Scheduler 关键入口、state 和 collections 已绑定固定 revision
- [ ] 能解释三个主要约束：sequences、token budget、KV capacity
- [ ] 四个确定性场景均有有效 trace，或明确记录无法触发的原因
- [ ] Trace parser 能检查 token 和 state invariants
- [ ] 至少观察一次 chunked prefill 或同等长 prompt 分步调度行为
- [ ] 至少观察一次 KV pressure 路径，且与 token pressure 分开
- [ ] 内部 trace 与 client TTFT/TPOT 建立可解释关联
- [ ] Week 9 的 KV cache manager 问题列表已形成
- [ ] GCP VM 已停止，trace 已同步并绑定 Git commit
