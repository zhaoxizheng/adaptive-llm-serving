# Week 14 Plan: Chunked Prefill、CUDA Graph 与 Parallelism 受控优化

> 时间预算：约 11 小时，包含约 2.5 小时的双卡实验窗口
>
> 本周主线：把 Week 11–13 的证据转成可重复的优化 A/B，选择供 Week 15 使用的单实例 operating point，而不是叠加所有优化开关。
>
> 前置：[Week 13 plan](week-13-plan.md) 的瓶颈证据和无 profiler baseline；阅读：[Week 14 references](week-14-references.md)。下列文件是待完成产出。

## 本周目标

1. 用 workload 分组结果解释 token budget 对 TTFT/TPOT 的权衡。
2. 隔离 CUDA Graph 与 compilation、batch shape 等混杂因素。
3. 区分 TP 的模型容量价值、延迟变化和 GPU 成本。
4. 选出满足 SLO、质量与显存约束的配置，而非只选 tokens/s 最大值。
5. 完成单实例阶段报告，为多副本实验冻结后端配置。

## 本周边界

- 单卡主线固定 Week 13 的硬件、模型、dtype、backend 和 arrival trace。
- 每组实验只改变一个主变量，候选确定后才做组合回归。
- Quantization 复用 Week 6 已验证结论，本周不再引入新量化格式。
- 不实现新 scheduler、kernel 或多节点 TP；不进入集群级网关/路由。
- 所有容量、延迟和成本结论来自无 profiler runs；短 profiling 仅解释差异。

## 本周最终产出

- `configs/week14-optimization.yaml`：baseline、chunk budget、graph modes 和 SLO。
- `configs/week14-tp.yaml`：双卡型号、拓扑、TP 配置与对照矩阵。
- `scripts/run_week14_optimization.sh`：分组 A/B、重复和失败退出。
- `results/week14/`：逐请求原始数据、配置、短 trace 与成本清单。
- `reports/week14.md`：性能 Pareto 对比、失败实验和最终选择。
- `configs/serving-baseline.yaml`：Week 15 固定的模型、engine args 和 workload contract。

## 三组实验

### A：Chunked prefill 与 token budget

- 固定 short/long prompt 比例、output lengths 和到达序列，至少覆盖 low-load 与 near-SLO 两档。
- 选择三个已通过显存 smoke 的 `max-num-batched-tokens` 值，固定 `max-num-seqs`、APC 和其他配置。
- 按 short/long 请求分别报告 P50/P95/P99 TTFT、TPOT、错误率和 goodput，不能只报告全体平均值。
- 用 scheduler trace 解释 chunk size、decode interleaving 和 queue 的变化。
- Chunked prefill on/off 只在固定版本支持且配置可比时补做；不为造对照修改引擎。

### B：CUDA Graph

- 使用相同 compilation 设置，优先比较受支持的 graph-disabled mode 与指定 graph-enabled mode。
- `--enforce-eager` 可能同时改变 compilation，不能未经核对就称为 graph-only A/B；无法隔离时将结果标成执行模式组合对比。
- 单独验证 fixed-shape steady decode 的 replay/launch pattern，再运行相同 serving arrival trace；后者 batch composition 是观测结果，不保证完全一致。
- 记录实际 dispatch mode、capture sizes、padding、startup/capture 时间与额外显存。
- Engine 可能按剩余显存自动调整 KV blocks：受控比较应在支持时固定相同 KV capacity，否则明确报告这一混杂因素。
- Warmup 和 graph capture 不计入稳态 latency，但纳入启动时间及 GPU 成本。

### C：Tensor parallel

租用同一台双卡机器，记录 GPU 型号、数量、互联、CPU、CUDA/NCCL 和模型版本。先做 TP=1、TP=2 的输出与通信 smoke。

| 对照 | 固定什么 | 可以回答什么 |
|---|---|---|
| 同一主机 TP=1 vs TP=2 | 模型、dtype、请求 trace；GPU 数不同 | 增加一张卡后的 latency、capacity 和成本变化 |
| TP=2 vs 两个独立 TP=1 replicas | 两张同型号 GPU、模型、总 offered load | Week 15 再做：同一 GPU 预算如何分配 |

- Week 14 不跨不同机型计算“TP speedup”；不能把单卡 L4 与另一种双卡硬件直接做因果对照。
- 模型必须同时能以 TP=1 和 TP=2 运行，且 head/partition 配置受支持；否则只报告模型容量验证。
- 记录 TP 通信区间、per-GPU memory、端到端 latency、总 throughput 和 GPU-seconds/request。
- 若没有双卡预算或 quota，完成配置、代码阅读和实验设计，将 TP 标记为 deferred，不编造多卡结果；Week 15 的真实双副本资源仍需单独满足。

## 统计与选择规则

- 每个候选至少三个独立重复，固定请求 trace 并交错运行 baseline/candidate，记录 clock、温度和其他进程。
- 先排除输出异常、OOM、持续 preemption 或错误率超预算的配置。
- SLO 沿用前周事先冻结的 TTFT/TPOT 阈值，不在看到数据后降低要求。
- `goodput = 同时满足既定 SLO 且成功的请求数 / 测量窗口秒数`；失败/超时不从 SLO attainment 的分母中消失。
- 保存实际请求数和重复间波动；小样本 P99 不用于宣称稳定容量。
- 候选组合需要重跑 short、long、mixed 与 shared-prefix 回归；各自有效不代表组合必然有效。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 固化 baseline/SLO 和单变量矩阵；准备双卡 quota、拓扑与预算检查 |
| Day 2 | 2 h | 完成 chunk budget sweep，按 short/long 分类输出 latency/goodput |
| Day 3 | 1.5 h | 完成 graph A/B，核对 dispatch、compilation 和 KV capacity |
| Day 4 | 1.5 h | 对候选做组合与负向回归，检查错误率、显存和统计波动 |
| Day 5 | 1 h | 整理单卡证据，准备 TP smoke 和统一双卡配置 |
| Day 6 | 2.5 h | TP=1/2 最小受控对比；无硬件则完成设计并明确 deferred |
| Day 7 | 1 h | 冻结 serving baseline、完成阶段报告、同步结果并停止计费资源 |

## 报告必须回答的问题

1. 更大的 token budget 帮助了谁，又损害了哪一类请求？
2. Graph 改变的是 launch overhead、execution mode、KV capacity，还是多个因素？
3. 每个优化的启动时间、稳态延迟和显存代价是什么？
4. TP 结论是在增加 GPU 预算下成立，还是同预算下已验证？
5. 最终配置在哪些 workload 不占优，是否仍满足 SLO？
6. 哪些变量必须固定，Week 15 才能把变化归因到路由或副本数？

## 完成标准

- [ ] Chunk budget 与 graph A/B 均有无 profiler 原始结果和分组指标。
- [ ] Compilation、shape、KV blocks 等混杂因素被控制或明确披露。
- [ ] 候选组合通过全部 workload 回归，失败和负收益也保留。
- [ ] 双卡 TP 有受控结果；硬件阻塞时单独标记未完成/deferred。
- [ ] 固定 serving baseline 可供下一周复用，没有偷偷变更模型或 dtype。
- [ ] Week 11–14 形成至少一条由 profiler 证据解释的服务优化结论；无收益也是有效结论。
- [ ] GPU、磁盘等成本已记录，原始结果同步并绑定 Git commit。
