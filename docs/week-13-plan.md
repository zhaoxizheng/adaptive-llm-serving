# Week 13 Plan: Kernel Profiling 与 Prefix Caching 专项

> 时间预算：约 11 小时
>
> 本周主线：从 Week 12 选出的热点 kernel 出发，用 Nsight Compute 验证瓶颈假设；另用无 profiler 的服务实验量化 prefix caching 收益，不把两种测量混为一谈。
>
> 前置：[Week 12 plan](week-12-plan.md) 的 timeline 与 kernel target；阅读：[Week 13 references](week-13-references.md)。下列文件是待完成产出，不代表仓库已有实现。

## 本周目标

1. 针对一个 kernel family 收集必要 counters，而不是 profile 整个 server。
2. 区分 arithmetic intensity、memory throughput、SM throughput 和 occupancy。
3. 说明 replay、cache control 与 clock policy 对测量的影响。
4. 用 cold/warm、shared/non-shared 对照量化 prefix caching 的适用边界。
5. 将 kernel-level 证据与 client TTFT、TPOT、goodput 分开报告。

## 本周边界

- 继续使用固定模型 revision、单卡 L4、dtype、attention backend 和 vLLM commit。
- 不写 CUDA/Triton kernel，不以提升 occupancy 本身作为优化目标。
- 不同时运行 Nsight Systems 和 Nsight Compute；counter capture 只用于机制分析。
- Prefix cache 是当前 vLLM 实例内的复用，不涉及跨实例 KV transfer。
- 只使用合成 prompt；结果和 trace 不包含凭证、私人对话或内部流量。

## 本周最终产出

- `configs/week13-kernel.yaml`：kernel selector、shape、NVTX range、sections 与 replay 设置。
- `configs/week13-prefix.yaml`：prompt families、arrival trace、cache 状态与重复次数。
- `scripts/run_week13_ncu.sh`：最小 counter smoke、定向 capture 与 export。
- `results/week13/ncu/`：原始 `.ncu-rep`、CLI、counter 表与环境清单。
- `results/week13/prefix/`：逐请求结果、cache 指标增量与 baseline。
- `reports/week13.md`：kernel 假设判定、prefix caching A/B 与 Week 14 handoff。

## Kernel Evidence Contract

| 项目 | 必须记录的证据 |
|---|---|
| 为什么选它 | Week 12 的累计 GPU 时间占比、调用次数和所在阶段 |
| 执行身份 | kernel 名称、shape、dtype、backend、launch 配置、进程/rank |
| Counter 范围 | ncu/GPU/driver 版本、实际 sections、支持的 metrics 与单位 |
| 采集扰动 | replay mode、passes、cache control、clock policy、capture window |
| 瓶颈判断 | compute/memory throughput、roofline 或 memory analysis，及可反驳该判断的证据 |
| 服务影响 | 单独采集的无 profiler TTFT、TPOT、throughput 与 goodput |

先检查云 GPU 是否允许访问 performance counters。遇到 `ERR_NVGPUCTRPERM` 时记录限制并请求管理员按平台规范处理；不为了实验全局放宽共享主机权限，也不把缺失 counter 写成已验证结论。

从 minimal sections 开始，按假设增加 Speed of Light、Memory Workload、Occupancy 或 Roofline 分析；名称和可用项以本机 `ncu --help`、section/metric 查询为准。低 occupancy 不足以证明 occupancy bottleneck，高 memory throughput 也不等于端到端服务受显存带宽限制。

## Prefix Caching 实验矩阵

使用两个固定到达速率：低负载和 Week 6 已确认的 near-SLO operating point。每个 cell 至少运行三个独立重复，保存实际请求数；样本不足时将 P99 标为探索性结果。

| 场景 | APC | Cache 初始状态 | 目的 |
|---|---|---|---|
| A | Off | 无复用 | 服务 baseline |
| B | On | 模型已 warm、prefix cache 未预热 | 区分 runtime warmup 与 cache cold start |
| C | On | 预填充固定 shared prefixes | 验证稳态命中与 prefill 节省 |
| D | On/Off | token 级低重合前缀 | 收益边界和额外开销对照 |

- A/B/C 使用相同 prompt、output length、arrival trace 和采样参数；prefix family 与长度在 tokenization 后确认。
- 模型 warmup 使用不与测量集共享的 token 前缀；如固定版本支持 cache reset，保存调用与 reset 证据，否则隔离运行并验证初始状态。
- B 的 cache 会在运行中逐步变暖，必须画出命中和 TTFT 随请求顺序的变化，不能把整段称为全冷。
- C 的预热请求不计入稳态 latency，但记录预热时间和 token 成本；计费仍包含该阶段。
- D 尽量匹配输入/输出长度，并测量残余公共 chat-template tokens，不能假设完全零命中。
- 保存当前版本 cache hit/query counters 的定义、单位与窗口增量；区分 token/block/request hit rate。
- 同时看 recomputed prefill tokens、TTFT、TPOT、queue、preemption 与 goodput。APC 不消除新 token 的 decode。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 复核 Week 12 target；counter 权限 smoke；冻结 kernel identity 和待验证假设 |
| Day 2 | 1.5 h | 在短窗口采集目标 kernel，检查 filters、worker 进程和 replay，导出最小报告 |
| Day 3 | 2 h | 加入必要 counter；分析 compute/memory/occupancy，和原始 timeline 对照 |
| Day 4 | 2 h | 构造 token 级 prefix families；运行 A/B/C 的低负载无 profiler 对照 |
| Day 5 | 1.5 h | 运行 D 和 near-SLO 对照，记录冷到暖过程与 cache churn |
| Day 6 | 1.5 h | 完成重复实验、检查输出和计数口径，生成两张图与 counter evidence table |
| Day 7 | 1 h | 完成报告和 Week 14 候选优化；同步结果、停止 GPU 并检查残余计费 |

GPU 只在 Day 1 smoke 和 Day 2–6 的实际执行窗口计费；阅读和分析在本地完成。

## 报告必须回答的问题

1. 选出的 kernel 占多少 GPU 时间，位于 prefill 还是 decode，值得优先优化吗？
2. Counter 支持哪种瓶颈解释，哪些替代解释仍未排除？
3. Replay、cache flush 或 clock 设置是否改变了待研究的执行条件？
4. APC 对低负载和 near-SLO 的 TTFT/goodput 收益是否一致？
5. Warm-cache 收益需要多少预热成本，低重合负载有无退化？
6. TPOT 的变化来自 queue/batch composition，还是有独立执行证据？
7. 哪一个参数或执行模式最值得 Week 14 验证，为什么？

## 完成标准

- [ ] 目标 kernel 有原始 report、环境、shape 和明确 counter 语义；权限阻塞则标记未完成。
- [ ] Kernel 分析与无 profiler 服务结果分开，不能用 ncu duration 宣称容量提升。
- [ ] 四种 prefix 场景可复现，cache 初始状态和预热成本可追溯。
- [ ] A/B 使用相同请求 trace，失败/超时和样本量均进入报告。
- [ ] 至少一个瓶颈假设得到支持或被推翻，未决项明确。
- [ ] Week 14 的优化候选、基线配置和 SLO 已冻结。
- [ ] 原始结果同步完成，GPU 停止，报告绑定 Git commit。
