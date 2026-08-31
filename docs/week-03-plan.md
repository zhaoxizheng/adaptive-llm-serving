# Week 3 Plan: 动态 Batching、排队与尾延迟

> 时间预算：10–12 小时
>
> 本周主线：在 Week 2 静态 batch 基线之上，实现一个教学型 dynamic request batching scheduler。通过可重复的 Poisson 到达流量，定量观察 batch 等待、GPU service time、吞吐和 P95/P99 延迟之间的关系。

## 本周边界

本周实现的是 **request-level dynamic batching**：若干请求先进入队列，再组成一个静态 batch，一旦 dispatch，这个 batch 会一起执行到结束。它用于理解排队和组 batch 的代价。

它不是 vLLM 的 continuous batching，也不尝试复刻 iteration-level scheduler、PagedAttention、KV block 管理或 preemption。Week 4 会用 vLLM 观察这些机制带来的真实效果。

## 本周目标

完成本周后，应当能够：

1. 区分 client wait、queueing delay、batch formation delay、service time 和 E2E latency。
2. 解释 arrival rate、service capacity 和 queue stability 的关系。
3. 实现并比较 no batching、fixed window、size-or-time 三种策略。
4. 解释 batching 为什么可能提高吞吐，同时恶化低流量延迟或高流量尾延迟。
5. 用 P50/P95/P99、queue depth、batch fill ratio 和 rejection rate 描述系统，而不只报告平均值。
6. 识别 request-level batching 的局限，并写出迁移到 vLLM continuous batching 时要验证的假设。

## 本周最终产出

- `configs/week03.yaml`：arrival trace、队列和策略参数
- `src/workload.py`：可复现的 Poisson arrival trace 与请求长度生成
- `src/dynamic_batcher.py`：三种 batching policy 和有界队列
- `src/serve_week03.py`：producer、scheduler、单 GPU worker 与事件记录
- `src/analyze_week03.py`：按策略和 offered load 汇总指标
- `results/week03/raw/events.csv`：逐请求时间戳
- `results/week03/raw/batches.csv`：逐 batch 组成和执行数据
- `results/week03/figures/*.png`：负载、吞吐、排队和尾延迟图
- `reports/week03.md`：结论、限制和 Week 4 假设
- 不依赖 GPU 的 scheduler 单元测试，以及一个 GCP L4 smoke test

## 系统模型

```text
deterministic arrival trace
          |
          v
     bounded queue ---- full / admission timeout ---> rejected
          |
          v
   batching policy
   - no batching
   - fixed window
   - size or time
          |
          v
 single GPU worker
 batched prefill + decode
          |
          v
 request events + batch events
```

只使用一个 GPU worker，避免把多 worker 并行与 batching policy 混为同一变量。producer 根据预先生成的 trace 在 monotonic clock 上释放请求；所有策略复用完全相同的 trace。

## 三种策略的精确定义

### 1. No batching

- `max_batch_size = 1`
- 请求按 FIFO 顺序立即 dispatch
- 作为 latency 基线，不代表最高吞吐方案

### 2. Fixed window

- 每隔 `window_ms` 到固定边界时，从队列取最多 `max_batch_size` 个请求
- 低流量下，即使请求已到达也可能等待下一个边界
- 用于观察简单周期聚合的额外等待

### 3. Size-or-time

- 第一个请求进入空队列时启动计时器
- batch 达到 `max_batch_size` 时立即 dispatch
- 否则在最老请求等待达到 `max_wait_ms` 时 dispatch 当前 batch
- 这是本周主要策略，也是常见 dynamic batching 的最小抽象

策略必须是纯队列逻辑，与模型调用分离，才能在 Mac 上用虚拟时间和 fake worker 测试。

## 请求与时间戳契约

每个请求至少记录：

| 字段 | 定义 |
|---|---|
| `request_id` | trace 内稳定且唯一的 ID |
| `scheduled_arrival_ns` | trace 计划到达时刻 |
| `admitted_ns` | 成功放入队列的时刻 |
| `dispatch_ns` | 被 scheduler 放入 batch 的时刻 |
| `gpu_start_ns` | GPU worker 开始处理该 batch |
| `first_token_ns` | 第一个 token 可用的时刻 |
| `finished_ns` | 请求完成时刻 |
| `status` | `completed`、`rejected` 或 `failed` |
| `batch_id` | 所属 batch |
| `prompt_tokens` / `output_tokens` | 实际 token 数 |

派生指标：

```text
arrival_lag       = admitted - scheduled_arrival
queueing_delay    = dispatch - admitted
worker_wait       = gpu_start - dispatch
service_time      = finished - gpu_start
ttft              = first_token - admitted
e2e_latency       = finished - admitted
batch_fill_ratio  = actual_batch_size / max_batch_size
```

`arrival_lag` 必须单独报告。若 Python producer 本身跟不上 trace，不能把这段时间误归因于 scheduler。

## Workload 与实验矩阵

### 校准阶段

先用 Week 2 的固定 shape `prompt=256, output=64` 测出：

- batch 1/2/4/8 的 median service time
- 各 batch size 的 observed requests/s
- 不发生持续排队时的近似可服务速率

### 主实验：固定请求 shape

保持 `prompt=256, output=64`，避免 padding 和长度差异干扰 batching policy。

```yaml
arrival_process: poisson
duration_seconds: 120
warmup_seconds: 20
seed: 42
offered_load_ratio: [0.25, 0.50, 0.75, 0.90, 1.05]
policies: [no_batching, fixed_window, size_or_time]
max_batch_size: [4, 8]
max_wait_ms: [2, 5, 10, 20]
queue_capacity: 128
admission_timeout_ms: 50
repeats: 3
```

`offered_load_ratio` 相对于校准得到的基准 capacity 生成，而不是拍脑袋指定 requests/s。1.05 用于有界过载实验，只运行足够观察 queue growth 和 rejection 的短时间。

### 选做：Mixed lengths

在主实验结论完成后，再加入两类请求：

- short：prompt 128、output 32
- long：prompt 1024、output 128

只把它用于发现 head-of-line blocking 和 padding waste，不与固定 shape 主实验混合汇总。

## 每日安排

### Day 1：排队模型和指标设计（约 1.5 小时，本地）

- [ ] 阅读 dynamic batching、Orca 和 tail latency 的核心资料
- [ ] 画出 request lifecycle 并冻结所有时间戳定义
- [ ] 写出三种 policy 的状态转换和 flush 条件
- [ ] 明确 overload 时使用有界队列与 admission timeout
- [ ] 从 Week 2 结果估算第一版服务 capacity

验收：给定五个请求的到达时间，能手工推出三种策略各自的 batch 和 dispatch 时间。

### Day 2：Workload generator（约 1.5 小时，本地）

- [ ] 使用固定 seed 生成 exponential inter-arrival time
- [ ] 将 arrival trace 预先写入 JSONL/CSV
- [ ] 不在运行过程中使用随机数决定下一次到达
- [ ] 实现 fixed-shape 与 mixed-length workload schema
- [ ] 测试相同 seed 产生完全一致的 trace

验收：三种策略可以读取同一份 trace 做公平对比。

### Day 3：Scheduler 与测试（约 2 小时，本地）

- [ ] 实现 bounded `asyncio.Queue`
- [ ] 实现 no batching、fixed window、size-or-time
- [ ] 使用 `time.monotonic_ns()`，不使用 wall clock 计算 duration
- [ ] 用 fake clock/fake worker 测试 size flush、timeout flush、FIFO 和 shutdown
- [ ] 验证取消和异常不会遗留未完成 request

验收：不启动 GPU 即可确定 batch 边界和时间语义正确。

### Day 4：接入 Week 2 GPU worker（约 2 小时，本地 + 0.5 小时 GPU）

- [ ] 将 batching policy 与模型执行分层
- [ ] 接入 batched prefill 和 greedy decode
- [ ] 为每个请求记录 first-token 与 completion event
- [ ] 逐请求和逐 batch 增量落盘
- [ ] 在 GCP L4 上跑 20 个请求 smoke test
- [ ] 检查事件时间顺序 invariant

### Day 5：主实验（约 2–3 个计费小时）

- [ ] 重新校准本次 VM 的 batch service capacity
- [ ] 先跑 0.25、0.75 和 1.05 三档 smoke matrix
- [ ] 再完成全部 offered load 与策略组合
- [ ] 每个组合使用同一 arrival trace 并重复 3 次
- [ ] 每完成一个 case 立即落盘
- [ ] 同步结果并停止 Spot VM

### Day 6：分析（约 1.5–2 小时，本地）

至少生成：

1. `offered-load-vs-throughput.png`
2. `offered-load-vs-p95-p99-ttft.png`
3. `batch-window-vs-fill-and-queue-delay.png`
4. `queue-depth-over-time-overload.png`

同时报告 achieved throughput、completion rate 和 rejection rate，不能只画成功请求的 latency。

### Day 7：报告和 Week 4 假设（约 1–1.5 小时，本地）

- [ ] 找到每种策略开始持续排队的 offered load
- [ ] 比较低负载下 batching 的固定等待成本
- [ ] 比较接近饱和时 P50 与 P99 的分叉
- [ ] 解释最大 batch 未装满时的吞吐损失
- [ ] 写出至少三个将在 vLLM 上验证的假设

建议假设：

1. vLLM continuous batching 在混合 output length 下比本周 request-level batching 更少浪费 decode slot。
2. vLLM 在相同 offered load 下可以推迟 queue instability 出现的边界。
3. vLLM 的内部 queue time 和本周 client-observed waiting time 不能直接混为同一指标。

## 公平性与安全检查

- 所有策略使用相同模型 revision、dtype、GPU、prompt/output tokens 和 arrival trace。
- benchmark 前 warm up；统计窗口不包括 server startup 和模型加载。
- client concurrency 足以发送 trace，但不能让 client event loop 成为瓶颈。
- 超过队列容量时显式 reject，不允许无限积压直到 VM 被停止。
- timeout 后记录状态并清理资源，不能把超时请求悄悄算作零延迟。
- Spot 抢占后按 case 恢复，不拼接不完整时间窗口。

## 本周不要做什么

- 不把教学 scheduler 宣称为 vLLM 的简化复刻。
- 不实现 iteration-level scheduling、KV block allocator 或 preemption。
- 不用无限队列掩盖 overload。
- 不用平均 latency 代替 P95/P99 和 rejection rate。
- 不在同一主实验中同时改变 arrival rate、batch size、wait window 和请求长度。
- 不在 Mac 上加载模型或跑正式 benchmark。

## 完成标准

- [ ] 三种策略通过 deterministic scheduler tests
- [ ] 相同策略和 trace 可以复现相同 batch membership
- [ ] 每个请求和 batch 都有完整事件记录
- [ ] 至少找到一个吞吐改善但 P99 恶化的配置
- [ ] 至少找到一个进入 overload 后 queue 持续增长的配置
- [ ] 报告包含 rejection、失败和 arrival lag，而不只展示成功样本
- [ ] 明确写出 request-level batching 与 continuous batching 的差异
- [ ] GCP VM 已停止，结果已同步并绑定 Git commit
