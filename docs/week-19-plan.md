# Week 19 Plan: 最终项目 Contract 与离线策略验证

> 时间预算：约 11 小时
>
> 本周主线：从 Week 17–18 的真实反例中选一个窄问题，冻结最终项目的 SLO、对照与数据分组，再离线验证最小路由策略；不重新搭建 serving 平台。
>
> 前置：[Week 17 plan](week-17-plan.md) 的 scaling 证据和 [Week 18 plan](week-18-plan.md) 的 locality/load 反例；阅读：[Week 19 references](week-19-references.md)。下列文件是待完成产出。

## 本周目标

1. 形成一个可证伪的问题，而不是先给最终项目设定提升百分比。
2. 定义 request-level SLO、goodput、测量窗口和成本分母。
3. 区分 calibration、development 和 held-out evaluation 数据。
4. 用统一单位和固定快照验证 queue/cache-aware score 的决策行为。
5. 准备 Week 20 可直接实现的最小 policy contract 与测试用例。

## 本周边界

- 本周以本地分析为主；GPU 只用于确有缺口的短 calibration，不跑最终完整矩阵。
- 不训练预测模型、不新建 autoscaler、不实现分布式 KV cache 或多租户调度器。
- 不重读整套 vLLM/AIBrix 文档；只围绕已发现的反例查证。
- Week 20 先固定副本验证路由，路由与 autoscaling 联合评估留到 Week 21。
- 若没有可重复的反例，先补证据或完成基线复现，不强行宣称自定义策略有必要。

## 本周最终产出

- `docs/experiment-methodology.md`：问题、SLO、数据分组、比较原则与停止条件。
- `docs/routing-policy-contract.md`：输入/输出、单位、候选过滤、失败行为和不变量。
- `configs/week19-study.yaml`：选定 baseline、校准参数、workload seeds 和样本预算。
- `benchmark/workloads/`：版本化合成 trace 清单与 split manifest。
- `router/policy/`：可离线回放的最小决策函数及表驱动用例，不接线上路径。
- `reports/week19.md`：问题证据、设计取舍和 Week 20 验收门槛。

## 问题选择

| 来自前周的观察 | 候选假设 | 反驳条件 |
|---|---|---|
| 热 prefix Pod 排队长 | 限制 cache affinity 的等待代价可改善短请求 TTFT | 同 GPU 下并无改善，或 low-sharing 明显退化 |
| 相同 request count 的 Pod token load 不同 | 加入已校准的工作量估计比仅数请求更有效 | 估计误差和开销抵消收益 |
| Metric/index 更新慢 | 有界 freshness 检查减少错误 affinity | 大部分请求退回 baseline，复杂度无收益 |

只选一行作为主问题，其他保留为观察。与 Week 16 的现有 load gate/blending 对照，确认候选不是重新实现已启用的能力。

## SLO 与数据 Contract

- 对每个有效 offered request 记录到达、首 token、完成、output token 数、错误/超时和所属 workload。
- TTFT、TPOT 阈值及目标达标比例在评价前冻结；TPOT 对 output tokens ≤ 1 的规则单列，不用除零结果或静默丢样本。
- `SLO attainment = 同时成功且满足该请求适用阈值的请求数 / 有效 offered requests`。
- Goodput 同时明确 cohort 与时间口径：固定 arrival window 的达标请求率另报 drain time；完成吞吐使用从测量开始到最后完成/统一超时的完整时长，不能互换分母。
- 只对成功完成请求计算 latency quantiles 时，同时报告 timeout/error；不能通过丢弃慢失败请求改善 P99。
- 主结果按 workload 和 short/long 分组，租户分组可作观察，但本周不声称实现 fairness。
- Calibration 用于拟合常数，development 用于有限候选选择，held-out seeds/prefix families 在参数冻结前不用于调优。
- 固定重复数与最大 GPU-hours；小样本 P99 标为探索性，不以看到显著收益作为停止条件。

## 最小 Policy Contract

基于校准数据提出一个候选，例如在 Ready、模型匹配、未 draining 的候选中最小化：

```text
estimated_ttft_ms(i) = estimated_queue_delay_ms(i)
                     + estimated_uncached_prefill_ms(i, request)
```

- Queue 与 uncached prefill 使用同一单位；缓存收益已经体现在 uncached tokens 中，不再重复减一次 cache reward。
- Queue delay 来自可在决策时获得的快照及 calibration，不使用请求未来实际完成时间；不可观测字段不得用离线全知数据补齐。
- 原 roadmap 的 weighted score 是候选形式，不必实现所有项；若用 normalized score，写清归一化区间、权重及裁剪规则。
- 给所有 Pod 加同一个 tenant penalty 不会改变排序，也不提供公平性；租户优先级/配额需单独机制和评价，本周不伪装成已实现。
- 输入包含 Pod UID、metric timestamp、model/tokenizer identity 和 cache confidence；输出是合法 Pod 或显式无候选，不能返回任意地址。
- 缺失/过期信息采用冻结的、已验证的 ready-only baseline；没有 Ready Pod 则显式失败，不回退到不健康实例。
- Snapshot replay 只检验排序和不变量：改变路由后未来 queue/cache 会变化，不能用旧 trace 反事实宣称新策略的线上 latency。

## 对照与消融设计

| 配置 | Week 20 | Week 21 |
|---|---|---|
| 同 gateway 的既有 load-aware 策略 | 固定双副本 baseline | 搭配冻结的 scaling 配置 |
| 已验证 prefix-aware 策略 | 固定双副本对照 | 按需要保留 |
| 最小候选策略 | 固定双副本 A/B | 再接同一 scaling 配置 |
| 候选关闭 cache term 或 freshness 处理 | 只做与主假设对应的一项消融 | 扩展完整矩阵 |

全部固定模型、GPU、engine config、附加 gates 和 cache 初始化。主实验不加入优先级、quantization 等新变量；对不同 GPU 数量的结果分别报告 allocated/billed GPU-hours。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 从前周报告选一个反例，写出假设与反驳条件 |
| Day 2 | 1.5 h | 冻结 SLO、失败口径、测量窗口与成本分母 |
| Day 3 | 2 h | 划分 calibration/development/held-out traces，确认无数据泄漏 |
| Day 4 | 2 h | 实现离线 score 和快照回放，必要时短时补 calibration |
| Day 5 | 1.5 h | 候选边界、stale/missing、tie 和无 Ready Pod 的表驱动用例 |
| Day 6 | 1.5 h | 预设重复/统计方法、消融和停止条件，冻结参数 |
| Day 7 | 1 h | 完成两份 contract 与报告，将用例交给 Week 20；同步结果和成本 |

## 报告必须回答的问题

1. 选择了哪个可重复反例，现有策略为什么没有解决？
2. 每个 SLO/goodput 指标的分子、分母、窗口和失败规则是什么？
3. 候选用了哪些在决策时真实可获得的信息？
4. Score 的量纲、cache 收益和 freshness 处理是否自洽？
5. Calibration/validation 数据如何隔离，如何防止对测试集调参？
6. 离线结果证明了什么，哪些性能结论必须等待真实 A/B？

## 完成标准

- [ ] 一个主假设、有证据的反例、预设反驳条件和有限实验预算。
- [ ] SLO、cohort、drain、失败和成本口径均已冻结。
- [ ] 数据 splits 可复现，最终评价数据未用于拟合参数。
- [ ] 最小决策函数通过关键不变量用例，没有未来信息或重复计算 cache 收益。
- [ ] 消融只针对主假设，不叠加新的控制变量。
- [ ] 未把离线回放或三个重复的波动范围当作稳定 P99/显著性结论。
- [ ] Week 20 的输入、集成边界、验收门槛和已知限制齐全。
