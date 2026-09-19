# Week 17 Reference Reading

第十七周只新增 metric-based autoscaling 的执行细节。AIBrix 总览和 Kubernetes HPA 已分别收录于 Week 16 与 Week 15，不再次列入新增书目。

新增页面于 2026-09-19 通过公开文档只读抓取核对；固定版本的 CRD、metric mapping 和运行采样优先于移动的 `latest` 示例。

## 新增必读

1. [AIBrix Metric-based Autoscaling](https://aibrix.readthedocs.io/latest/features/autoscaling/metric-based-autoscaling.html)
   - 重点读 Configurable metric windows、Supported PodAutoscaler annotations、metric sources 与结果分析。
   - 选择一个策略深入，核对 HPA/KPA/APA 不同控制路径和窗口，不逐个部署全部示例。
   - 示例的 cache target 可能混用比例与百分比；先验证实际单位，不能照抄阈值。

## 复用：只查本周增量问题

| 已有来源 | 本周只查什么 |
|---|---|
| [Week 16 references](week-16-references.md) #7 | PodAutoscaler target、engine-neutral metric 名称与数据来源 |
| [Week 15 references](week-15-references.md) #2 | CPU HPA 的 requests 分母、未 Ready Pods、stabilization 和缺失指标 |
| [Week 15 references](week-15-references.md) #1 | 扩容后的 readiness 与缩容 drain，不重学 Pod 生命周期 |
| [Week 4 references](week-04-references.md) #7 | 原始 running/waiting/cache metric 的类型和单位 |
| [Week 2 references](week-02-references.md) #3 | Goodput 口径，不重复学习 prefill/decode disaggregation |

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 新增 1；复用 Week 16 #7 | CRD、metric contract 与控制链 |
| Day 2 | 复用 Week 4 #7 | 指标校准和阈值 |
| Day 3 | 复用 Week 15 #2 | CPU HPA 与固定副本对照 |
| Day 4 | 新增 1 的窗口/annotation 章节 | 一个 AIBrix 策略的响应 |
| Day 5 | 复用 Week 15 #1–2 | 缺失指标、降载与 drain |
| Day 6–7 | 实验数据；按需回查 1 | SLO、震荡和成本结论 |

## 阅读后的自测问题

1. AIBrix 逻辑 metric 名称与 vLLM 原始名字为什么可能不同？
2. 瞬时 queue、窗口平均值和累计 counter 可以直接互换吗？
3. Stable/panic window、推荐周期和 Pod 启动时间如何共同影响 burst？
4. 为什么 missing metric 不能当作零负载？
5. CPU HPA 对比 queue-driven KPA，是否隔离了算法变量？
6. Pod 缩容而节点不缩容时，哪种成本指标会下降，哪种不会？
