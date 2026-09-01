# Week 5 Plan: 可观测性、SLO 与单实例容量边界

> 时间预算：8–10 小时
>
> 本周主线：把 Week 4 的一次性 benchmark 升级为可复现的观测体系。对齐客户端结果、vLLM `/metrics`、GPU 指标和 server log，用明确 SLO 定义 goodput，并找出单实例从稳定运行进入排队和过载的边界。

## 本周目标

完成本周后，应当能够：

1. 区分 client-observed latency、server queue time、model execution time 和网络开销。
2. 识别当前 vLLM 版本的 metric contract，并将已有 Prometheus 查询绑定到该版本。
3. 为 short chat、long context、generation 和 mixed workload 定义不同 SLO。
4. 将 benchmark run、Prometheus 区间、GPU 采样和 Git commit 用同一个 `run_id` 对齐。
5. 用 goodput、error rate、P99 和 queue growth 共同判断 capacity，而不是只看峰值 tokens/s。
6. 产出一个可以被 Week 6 参数实验复用的单实例 baseline。

## 本周边界

- 固定 Week 4 已选出的模型、revision、dtype 和主要 engine 参数。
- 本周改变的是 offered load，不同时做量化或大规模 engine 参数 sweep。
- Prometheus、PromQL、Grafana 和告警基础视为已掌握，只做 vLLM metric discovery 与 run-level 对齐。
- Grafana 用于观察和复核；正式结论必须能够从保存的原始结果与 Prometheus 查询重建。
- GPU utilization 只能说明设备忙碌程度，不能单独证明 scheduler 或 kernel 是瓶颈。
- 不引入 Kubernetes、多 replica、AIBrix 或公网 load balancer。

## 本周最终产出

- `configs/week05.yaml`：workload、load sweep、SLO 和采样参数
- `observability/vllm-metrics.md`：当前 vLLM 版本的 metric、label、unit 与查询映射
- `observability/prometheus/queries.yaml`：实验使用的版本化 PromQL
- `src/capture_run_metrics.py`：按 run 时间窗保存 Prometheus 查询结果
- `src/analyze_week05.py`：计算 capacity、goodput 和 SLO violation
- `results/week05/raw/`：client JSON、metrics、GPU sample、server log 和 metadata
- `results/week05/figures/`：负载、延迟、队列、goodput 与 GPU 图
- `reports/week05.md`：单实例容量边界和证据链

## 观测数据模型

每次运行至少生成：

```text
run_id
├── metadata.json          model, revision, commit, GPU, server args
├── client.json            per-request timestamps and outcome
├── benchmark-summary.json aggregate client metrics
├── prometheus.json        range-query results
├── gpu.csv                timestamped GPU utilization and memory
└── server.log             startup, warning, preemption and error evidence
```

所有数据使用 UTC 时间戳。记录 benchmark 的 warmup 起点、measurement 起点和结束时间；Prometheus 查询只汇总 measurement window，不把模型加载和 warmup 混入 steady state。

## 指标契约

### 客户端指标

- offered request rate
- completed request throughput
- input / output token throughput
- P50/P95/P99 TTFT、TPOT 和 E2E latency
- success、timeout、HTTP error 和 invalid response count
- goodput：同时满足成功条件与该 workload SLO 的请求速率

### Server 指标

从当前安装版本的 `/metrics` 实际输出发现 metric 名，不在代码里假定某个版本的完整名称。至少覆盖：

- running / waiting requests
- request queue time
- prompt / generation token throughput
- KV cache usage
- request success / failure
- prefix cache hit（只作背景观测，不改变本周配置）

### GPU 辅助指标

- GPU utilization
- framebuffer memory used
- power draw
- SM / memory activity（若当前工具可以稳定获取）

GPU 采样必须带时间戳。不要把 1 秒采样的瞬时值与单请求微秒级阶段做伪精确对齐。

## SLO 与 Goodput

在 `configs/week05.yaml` 中显式记录每类 workload 的 SLO，例如：

```yaml
slos:
  short_chat:
    ttft_ms: 500
    tpot_ms: 50
    e2e_ms: 2500
  long_context:
    ttft_ms: 2000
    tpot_ms: 60
  generation:
    ttft_ms: 800
    tpot_ms: 60
```

这些数字是实验 contract，不是通用行业标准。正式运行前根据 Week 4 的低负载 baseline 校准一次，并在同一轮实验中保持不变。

单请求判定：

```text
slo_attained = success
               AND ttft <= workload.ttft_slo
               AND tpot <= workload.tpot_slo
               AND e2e <= workload.e2e_slo (if configured)

goodput_rps = count(slo_attained requests) / measurement_seconds
```

## Capacity 判定规则

一个 load point 只有同时满足以下条件，才算稳定：

1. error rate 和 timeout rate 不超过配置阈值。
2. P99 TTFT、TPOT 和 E2E 满足对应 SLO。
3. measurement window 后半段的 waiting queue 没有持续增长。
4. achieved throughput 能跟上 offered load，而不是靠请求堆积制造繁忙假象。
5. 至少 3 次重复中有一致结论。

最大稳定 load point 与第一个不稳定 load point 一起报告。不要用插值伪造未测量的精确 capacity。

## 每日安排

### Day 1：冻结 baseline 与 metric inventory（约 1–1.5 小时）

- [ ] 固定 Week 4 operating point、模型 revision 和 vLLM 版本
- [ ] 保存 `vllm serve --help`、`vllm bench serve --help` 和完整 server args
- [ ] 启动 smoke server，保存一次原始 `/metrics`
- [ ] 将实际 metric 名、类型、单位和 label 写入 inventory
- [ ] 确认每个 client result 都有 request ID 和 timestamp

验收：能够解释每个进入正式报告的指标从哪里产生、单位是什么。

### Day 2：接入已有观测栈（约 1 小时）

- [ ] 将 vLLM target 接入已有 Prometheus 并验证 target health
- [ ] 核对 scrape interval、时钟与 metric labels
- [ ] 保存 request、latency、queue、KV cache 和 token rate 查询
- [ ] 在已有 Grafana 中做最小面板或临时 Explore 复核
- [ ] 不安排 Prometheus/Grafana 基础教程或 dashboard 美化

Histogram 百分位使用 `histogram_quantile()` 和 bucket rate 计算；若 vLLM 当前版本提供 native histogram 或不同 metric contract，以该版本官方说明和实际 exposition 为准。

### Day 3：Run-aligned capture（约 1–1.5 小时）

- [ ] 实现按 measurement time window 查询 Prometheus
- [ ] 同时启动和停止 GPU 采样
- [ ] 将 benchmark config、server config、commit 和时间窗写入 metadata
- [ ] 设计失败时也能保留 partial evidence 的目录结构
- [ ] 用一个 20-request run 做端到端验证

### Day 4：Short chat capacity sweep（约 1.5 个计费小时）

- [ ] 从 Week 4 稳定速率的约 25% 开始
- [ ] 逐步运行 50%、75%、90%、100%、110% 和 125% load
- [ ] 每个 case 包含 warmup、固定 measurement duration 和 cooldown
- [ ] 在拐点附近增加 1–2 个点，而不是无限扩大矩阵
- [ ] 保存 timeout、失败和不满足 SLO 的请求

### Day 5：Mixed workload 与过载（约 1.5–2 个计费小时）

- [ ] 使用固定 seed 生成 prompt/output length mixture
- [ ] 保持 mixture 不变，仅改变 arrival rate
- [ ] 观察 short request 是否被 long request 影响
- [ ] 记录 queue 是否在 offered load 超过 service rate 后持续增长
- [ ] 设置最大运行时间和客户端 timeout，避免失控排队

### Day 6：Goodput 与交叉验证（约 1 小时）

- [ ] 从 per-request 数据独立计算 SLO attainment
- [ ] 比较 client completed throughput 与 server token counters
- [ ] 检查 Prometheus reset、缺采样和 scrape gap
- [ ] 比较 Grafana 面板、离线分析和原始数据是否一致
- [ ] 对异常 run 标记 invalid，不静默删除

### Day 7：报告与 Week 6 baseline（约 1–1.5 小时）

至少生成：

1. `offered-load-vs-achieved-throughput.png`
2. `offered-load-vs-p99-ttft.png`
3. `offered-load-vs-queue-depth.png`
4. `offered-load-vs-goodput.png`
5. `run-timeline-client-server-gpu.png`

报告中明确选出 Week 6 的低负载、SLO 边界和过载三个比较点。

## 本周不要做什么

- 不用 Grafana 截图代替原始数据。
- 不对 cumulative histogram bucket 直接求平均。
- 不忽略 timeout 后只计算成功请求的漂亮 P99。
- 不用 GPU utilization = 100% 直接推导“计算瓶颈”。
- 不在 benchmark 过程中改变 dashboard 查询或 server 参数。
- 不将模型启动、下载或 CUDA graph capture 混入 steady-state latency。

## 完成标准

- [ ] 已有 Prometheus 能稳定抓取 vLLM，版本化查询可从仓库复用
- [ ] client、server、GPU 和 log 可按 `run_id` 与 UTC 时间窗对齐
- [ ] 至少完成 short chat 和 mixed 两类 load sweep
- [ ] 每个 load point 有至少 3 次有效重复
- [ ] goodput 的计算可以从 per-request 数据独立复核
- [ ] 找到最大稳定点和第一个不稳定点
- [ ] Week 6 的三个固定 load point 已写入配置
- [ ] GCP VM 已停止，结果已同步并绑定 Git commit
