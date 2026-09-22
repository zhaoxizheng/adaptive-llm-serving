# Week 20 Plan: HPA/KEDA 扩缩容与可观测性（WVA 选做）

> 时间预算：约 11 小时
>
> 本周主线：在 Week 19 固定 release 与资源图上，让 HPA 经固定 metrics adapter、KEDA 经 Prometheus scaler 读取同一推理压力语义，分别验证单一副本写入链路、冷启动与成本口径；仅在版本兼容且主矩阵完成后选做 Workload Variant Autoscaler（WVA）。
>
> 前置：[Week 15 plan](week-15-plan.md) 的 HPA/冷启动基线与 [Week 19 plan](week-19-plan.md) 的 `LLMInferenceService` 资源/ownership contract；阅读：[Week 20 references](week-20-references.md)。下列文件均为计划产出，不代表 autoscaler 已部署或已证明节省成本。

## 本周目标

1. 为一个推理压力指标冻结 query、单位、series selection、聚合、freshness 与缺失行为。
2. 固定 Prometheus adapter 的 query-to-metric mapping 和 API discovery，再在 HPA 与 KEDA 两条互斥路径中确认唯一 replicas writer。
3. 以相同 vLLM/Gateway/GAIE/模型和 workload 比较固定副本、HPA 与 KEDA。
4. 验证 metric outage、冷启动、长 stream drain、cooldown 和 scale-to-zero/idle 的真实边界。
5. 分开报告 allocated GPU-hours 与 billed node/GPU-hours；Pod 缩容不自动等于云账单下降。
6. 若固定 release 支持 WVA，再选做验证“WVA recommendation → 恰好一个 HPA 或 KEDA actuator pass-through”链路，不替代主矩阵。

## 本周边界

- 主矩阵只用一种预先校准的指标，不同时搜索多个 PromQL、阈值和窗口。
- HPA 路径必须通过固定版本的 adapter/provider 将该指标暴露到 `custom.metrics.k8s.io` 或 `external.metrics.k8s.io`，并保存 API discovery、映射规则和 HPA `currentMetrics`；若该链路不可用，HPA 只能降级为 Week 15 CPU baseline，不能声称与 KEDA 做同指标比较。
- 每个 workload 任一时刻只能有一个 desired-replicas source 和一条 writer path；独立 HPA、KEDA `ScaledObject`、WVA recommendation、GitOps/manual replicas 不得同时竞争。
- HPA 与 KEDA 改变了控制实现，不能把差异自动归因于“算法更智能”；逐段对齐指标和 scaling behavior。
- Scale-to-zero/idle 仅在入口、激活指标、模型加载和首请求语义都可验证时做隔离实验，不放入默认 SLO 主矩阵。
- WVA 是可选路径：若所选 KServe API/WVA/controller 与 HPA/KEDA backend 不兼容，记录 blocker，不升级整套栈来追逐示例。
- 主实验预留同一 GPU 节点容量以隔离 Pod scaling；节点 autoscaling 与跨云成本优化不在本周范围。

## 本周最终产出

- `docs/autoscaling-contract.md`：计划记录 metric、target、writer ownership、窗口、fallback、cooldown 和成本口径。
- `deploy/autoscaling/`：计划保存 Prometheus adapter 配置、互斥的 fixed/HPA/KEDA overlays，以及兼容时的 optional WVA overlay。
- `configs/week20-autoscaling.yaml`：计划冻结 workload seeds、阈值、min/max、超时、重复数和停止条件。
- `dashboards/week20-autoscaling.json`：计划提供同一时间轴的 demand、metric、desired/current/Ready、请求 SLO 与资源状态。
- `scripts/run_week20_autoscaling.sh`：计划执行 ownership preflight、burst/ramp、outage、drain 和恢复。
- `results/week20/` 与 `reports/week20.md`：计划保存原始时间线、事件、逐请求结果、资源与账单证据。

## Unique-writer Contract

| 实验模式 | 唯一决策者/写入路径 | 必须禁用或移除 |
|---|---|---|
| Fixed | GitOps/manifest 中固定 replicas | HPA、ScaledObject、WVA scaling spec 和其他 controller |
| HPA | Metrics adapter 暴露同一语义的 custom/external metric；HPA controller 写 scale target | KEDA ScaledObject、WVA、持续覆盖 replicas 的 GitOps/manual loop |
| KEDA | KEDA 管 0↔1 激活并生成/管理 HPA 完成 1↔N | 独立 HPA、WVA、manual/GitOps replicas writer |
| Optional WVA + HPA/KEDA actuator | WVA 是 desired replicas 的唯一来源；选一个 actuator 传递 | 另一 actuator、独立 HPA/KEDA formula、固定 replicas |

每次切换先列出 `managedFields`、HPA/ScaledObject/VariantAutoscaling 与 scale target，确认旧对象消失或停止写入，再发流量。`spec.replicas`、`spec.scaling` 若在固定 CRD 中互斥，遵循实际 admission schema；不得绕过校验。移除 `spec.replicas` 或转换为 `spec.scaling` 后，还要验证 KServe reconcile 不再用旧 fixed value 覆盖选定 child scale target。

## Metric 与 Observability Contract

| 维度 | 必须冻结的内容 |
|---|---|
| 语义 | 原始 metric、含义、counter/gauge/histogram 类型，以及它是否代表 demand、queue 或 saturation |
| 选择 | PromQL、labels、model/namespace/Pod identity，防止抓入旧 Pod 或其他 workload |
| 单位 | requests/tokens、每秒/窗口值、比例 0–1 或百分比 0–100，threshold 与 target 同单位 |
| 聚合 | sum/avg/max、per-Pod 或 workload total、Ready/缺失 Pod 的分母 |
| 时间 | scrape、adapter/KEDA polling、HPA sync、freshness、window 与 dashboard query step |
| API 暴露 | adapter release/image、series/query mapping、APIService health、custom/external metric 名与 HPA `currentMetrics` |
| 异常 | empty vector、NaN/Inf、Prometheus 失败、stale series 与 fallback；都不能静默当零 |
| 关联 | request ID/run ID、metric sample、recommendation、desired/current/Ready 和 first successful token |

先在固定单副本上用 low/steady/backlog 三段校准，证明查询方向和数量级正确，再冻结 threshold。正式 evaluation 使用不同 seed；dashboard、adapter 和 KEDA 三处 query 必须语义等价，且从 aggregated API 返回值对账到 HPA `currentMetrics`。两条路径的 polling/sync 与 missing-data 机制仍不同，结论只能归因于完整控制路径。

## 最小实验矩阵

保持同一模型、Gateway/GAIE 路由、image digests、GPU 上限、arrival trace、cache warmup、SLO 和 client timeout。每个正式 cell 至少三个独立重复并交错运行。

| 配置 | 副本范围 | 回答的问题 |
|---|---:|---|
| Fixed-1 / Fixed-2 | 1 / 2 | 无控制延迟和预热容量上下界 |
| HPA + metrics adapter | 1–2 | HPA 经 custom/external metrics API 使用同一推理信号的完整路径 |
| KEDA | 1–2 | Direct Prometheus、polling/cooldown/fallback 的整体效果 |
| KEDA idle/zero | 0–2，隔离选做 | 激活到首 token 的冷启动与失败边界 |
| WVA + 单一 actuator | 1–2，兼容时选做 | WVA recommendation 到 actuator passthrough 是否符合固定 release contract |

轨迹使用 steady → sustained burst → recovery 和 slow ramp → unload；短于完整冷启动的 burst 单列。保存 observe → desired → scheduled → image/model ready → route eligible → first token 时间戳。发生 Pending、模型下载、quota 或 node provisioning 时单独归因，不能当作 controller 算法耗时。

### 冷启动与缩容检查

- 分解 Pod scheduled、image pull、model load、readiness、GAIE endpoint 可选与首个成功 token；Ready 不等于已有热 prefix cache。
- 若测试 scale-to-zero，确认请求入口在零 Pod 时仍可达、激活信号不依赖已消失的 Pod metric，并为第一批请求定义排队/超时规则。
- 在 metric outage 下验证 HPA Unknown/KEDA fallback 或固定 release 的实际行为；不得把 stale/empty query 转成低负载。
- 降载时保留长 SSE，请求 drain 与 termination grace 独立于 cooldown；终止中的 active request 不从失败分母删除。

## 成本 Contract

```text
allocated GPU-hours = integral(workload-allocated GPU count over time) / 3600
billed GPU/node-hours = provider billing or node lifecycle quantity over the same experiment boundary
```

同时记录 warmup、idle capacity、node provisioning、磁盘和 LoadBalancer 成本。若两个 GPU 节点全程存在，Pod 从 2 缩到 1 只能支持 allocation 利用率结论，不能声称账单降低。按成功且达标 token 报成本时，失败/超时仍保留在 SLO 分母。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 冻结 fixed/HPA/KEDA、metrics adapter、scale target 与 unique-writer preflight |
| Day 2 | 1.5 h | 校准 PromQL 与 adapter mapping，验证 aggregated API/HPA currentMetrics，建立 dashboard 时间轴 |
| Day 3 | 2 h | 运行 Fixed-1/2 与 HPA burst/ramp 基线，保存完整 cold-start chain |
| Day 4 | 2 h | 运行 KEDA 同一矩阵，验证 polling、cooldown 与 fallback |
| Day 5 | 1.5 h | 做 metric outage、长 stream drain；条件满足时隔离测试 idle/zero |
| Day 6 | 1.5 h | 完成重复与 allocated/billed 核算；兼容时做 WVA passthrough smoke |
| Day 7 | 1 h | 完成 contract/报告、恢复固定副本、同步结果并停止计费资源 |

## 报告必须回答的问题

1. 每个模式中谁计算 desired replicas、谁写 scale target，如何证明没有双 writer？
2. Dashboard、metrics adapter、HPA `currentMetrics` 与 KEDA 实际 query 是否同源、同单位、同过滤和同窗口？
3. 响应延迟分别花在 scrape/poll/sync、schedule、image/model load、Ready、route eligibility 和 cache warmup 的哪一段？
4. HPA 与 KEDA 的差异包含哪些指标/窗口/激活/fallback 变量，哪些结论不能归因于单一算法？
5. Metric missing/stale、短 burst、长 stream 和 scale-down 时有什么失败或震荡？
6. Scale-to-zero 是否真的可唤醒并服务第一批请求，还是只证明副本能降到零？
7. Allocated GPU-hours 变化是否转化为 billed node/GPU-hours 变化？
8. WVA 若执行，是否只做 recommendation source 且 actuator 为 pass-through；若未执行，具体兼容 blocker 是什么？

## 完成标准

- [ ] Fixed、HPA、KEDA 各自只有一个可证明的 replicas writer，切换前后对象和 managed fields 已保存。
- [ ] 从 fixed 转为 scaling 后，KServe reconcile 未用旧 `spec.replicas` 覆盖 chosen scale target。
- [ ] Adapter/provider 版本、APIService、query-to-metric mapping 和 HPA `currentMetrics` 可审计；若缺失则明确降级为不可做同指标比较。
- [ ] Metric query、labels、单位、聚合、freshness、threshold 和 missing/fallback contract 完整。
- [ ] 固定副本、HPA、KEDA 使用同一 workload/SLO/版本并有独立重复和失败样本。
- [ ] Observe-to-first-token 时间线可关联，冷启动未被压成单一 Ready 延迟。
- [ ] Outage、drain、cooldown 和可选 idle/zero 行为有证据或明确 blocker。
- [ ] Allocated 与 billed GPU/node-hours 分开，未用 Pod 缩容冒充账单节省。
- [ ] WVA 明确标为兼容时选做，未完成的 optional path 没有写成已实现。
- [ ] 恢复固定副本并同步证据，GPU、LB、磁盘和残留 autoscaling resources 已检查。
