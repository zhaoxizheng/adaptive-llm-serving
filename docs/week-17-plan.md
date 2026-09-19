# Week 17 Plan: Inference-aware Autoscaling 与冷启动

> 时间预算：约 11 小时
>
> 本周主线：固定 Week 16 的路由与模型，以一个已校准的推理压力指标驱动 AIBrix 扩缩容，区分控制器响应、Pod Ready、首个 token 和真实计费资源。
>
> 前置：[Week 16 plan](week-16-plan.md) 的兼容性矩阵、固定双副本结果；阅读：[Week 17 references](week-17-references.md)。下列文件是待完成产出，不代表已有实现。

## 本周目标

1. 追踪 metric collection → recommendation → scale target → Ready → serving 的完整链路。
2. 校准一个推理指标的单位、聚合方式、阈值与缺失处理。
3. 对照固定副本、已有 CPU-based HPA 和一个 AIBrix metric-based 策略。
4. 量化突发流量的 SLO、冷启动和扩缩容震荡，不预设新策略更优。
5. 区分 Pod 分配 GPU-hours 与云节点实际计费，避免虚报节省。

## 本周边界

- 固定模型/revision、GPU 型号、vLLM/AIBrix images、路由及所有 gate/blending 设置。
- 默认复用同型号 NVIDIA L4 的两个独占 GPU slots；主实验预先保留节点容量，隔离 Pod scaling 与 node provisioning。
- 只深入一个 AIBrix 策略，默认先评估 KPA；APA、GPU optimizer、预测扩缩容和 scale-to-zero 不作为本周任务。
- 同一个 workload 只允许一套有效的扩缩容控制链；切换时检查直接及生成的 HPA，避免两个控制器同时写副本数。
- 所有实验在独立集群，以合成流量、固定 GPU 上限和最大运行时间执行。

## 本周最终产出

- `configs/week17-autoscaling.yaml`：metric contract、路由快照、阈值、窗口、min/max 和停止条件。
- `deploy/autoscaling/`：CPU HPA 与 AIBrix PodAutoscaler 的互斥实验配置。
- `scripts/run_week17_autoscaling.sh`：baseline、切换验证、burst 和结果保存。
- `results/week17/`：逐请求数据、每次推荐、实际副本数、启动事件与成本清单。
- `reports/week17.md`：SLO/成本对照、迟滞来源和后续集成配置。

## Metric 与 Control Contract

| 维度 | 需要确认的内容 |
|---|---|
| 来源 | Pod 原始 metric、AIBrix 逻辑名称、字段映射与采集成功状态 |
| 单位 | requests/tokens、瞬时值/窗口统计、比例 0–1 或百分比 0–100 |
| 聚合 | per-Pod、平均或总和，以及 Ready/未 Ready/缺失 Pod 的分母 |
| 时间 | sample timestamp、freshness、observe/panic windows、推荐周期 |
| 执行 | target workload、当前/建议/实际/Ready replicas、限幅与稳定窗口 |
| 异常 | stale/missing 不等于零；超出可用容量的 Pending 不等于新增服务能力 |

优先从 running/waiting requests 中选一个在固定单副本下可校准的信号。只用 waiting queue 可能在 decode 很忙但无排队时过早缩容；缓存占用也不等于活跃需求，空闲 APC blocks 可持续驻留。先用低负载、稳态和积压三段证明指标能反映本 workload，再选择阈值，不从示例抄 `0.5` 或 `50`。

阈值在 calibration trace 上确定，正式评价使用不同到达 seed；文档中 engine-neutral 名称与原始导出名可能不同，必须核对固定版本的 schema、映射代码和实际采样值。不能把 TTFT histogram 的 `_sum` 或 `_count` 当作 P99 控制信号。

## 最小实验矩阵

统一 arrival trace、cache 初始状态、warmup、SLO 与 client timeout。使用“稳态 → burst → 恢复”和“缓慢爬坡 → 降载”两种轨迹，每种至少三个独立重复。

| 配置 | 路由 | 副本范围 | 回答的问题 |
|---|---|---:|---|
| Fixed-1 | Week 16 冻结策略 | 1 | 不扩容时的压力下界 |
| Fixed-2 | 相同 | 2 | 已就绪容量对照 |
| CPU HPA | 相同 | 1–2 | 复用 Week 15 控制策略，重跑同一路由 |
| AIBrix metric-based | 相同 | 1–2 | 新指标与控制链的整体效果 |

CPU HPA 与 KPA 同时改变了指标和算法，差异只能先归因于整套策略；若要区分二者，另加同指标/同窗口的控制组，不宣称单独证明某算法更快。复用旧结果必须满足同版本、路由和 workload，否则重跑。

- 记录 pressure observed → desired increase → scheduled → model loaded → Ready → first successful token；新实例的 cold prefix cache 单独观察。
- 将低于冷启动时间的短 burst 与持续 burst 分开，控制器更快不保证短 burst 能被新增实例吸收。
- 在隔离测试中暂停一个 metric 输入，观察是否误缩容；保留失败事实，不为得到好结果隐藏缺失数据。
- 降载后验证长 SSE stream 的 drain、scale-down cooldown 和副本往返次数；缩容失败不从总成本中删除。
- SLO attainment 的分母包含测量窗口内所有有效 offered requests，失败和超时不能被静默丢弃。

## 成本口径

`allocated GPU-hours = ∫ workload 占用的 GPU 数 dt / 3600` 是资源分配指标；`billed GPU/node-hours` 来自实际节点生命周期和计费清单。两个 GPU 节点一直保留时，Pod 从 2 缩到 1 不自动降低云账单。分别报告 warmup、空闲节点、磁盘/LB 成本；节点扩缩容收益留给后续独立实验。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 冻结路由与 metric contract，核对 CRD、单位、GPU 上限和唯一控制链 |
| Day 2 | 1.5 h | 单副本校准指标与阈值，准备独立评价 traces |
| Day 3 | 2 h | 固定单/双副本及 CPU HPA 基线，保存启动与客户端时间线 |
| Day 4 | 2 h | AIBrix 策略运行同一矩阵，分解响应延迟 |
| Day 5 | 1.5 h | 降载、drain 和 stale/missing metric 验证 |
| Day 6 | 1.5 h | 完成必要重复，计算 SLO、震荡与两种 GPU-hours |
| Day 7 | 1 h | 冻结结论和配置，恢复固定双副本供 Week 18 使用，同步结果并停止计费资源 |

## 报告必须回答的问题

1. 控制器读到的指标和图表展示的指标是否同名、同单位、同窗口？
2. 延迟主要来自 metric、控制窗口、调度、模型加载还是 cache 变暖？
3. 哪类 burst 得到改善，哪类短 burst 来不及响应？
4. CPU HPA 与 AIBrix 对照改变了哪些变量，不能推出什么结论？
5. 缺失指标与长请求是否引起误缩容或震荡？
6. SLO 改善增加了多少 allocated GPU-hours，实际账单是否改变？

## 完成标准

- [ ] 校准与评价 traces 分开，metric 单位、来源和缺失处理可追溯。
- [ ] 四组配置有逐请求结果、实际 sample size 和 desired/Ready 时间线。
- [ ] 同时写副本数的控制器冲突已排除。
- [ ] Burst、降载、stale metric 与 drain 行为均有证据或明确 blocker。
- [ ] 资源分配与计费口径分开，负收益和无收益也保留。
- [ ] Week 18 恢复固定副本，后续闭环使用的 scaling 配置已冻结。
- [ ] 结果绑定 Git commit，GPU、磁盘与 LB 残余计费已检查。
