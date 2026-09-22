# Week 20 Reference Reading

第二十周新增 Prometheus Adapter、KEDA 控制链、Prometheus scaler 和 KServe Workload Variant Autoscaler（WVA）集成；Kubernetes HPA、Pod lifecycle 与 vLLM metric 语义全部复用前周资料。WVA 只在固定 KServe release 兼容时选做。

以下官方页面于 2026-09-22 只读核对。KEDA 的 `latest` 与 KServe 默认文档会移动，执行时固定 release/tag、CRD schema、controller image digest 和生成的 HPA/ScaledObject。

## 新增必读

1. [KEDA: Scaling Deployments, StatefulSets and Custom Resources](https://keda.sh/docs/latest/concepts/scaling-deployments/)
   - 阅读 `ScaledObject`、`scaleTargetRef`、polling/cooldown、idle replicas、fallback 和 generated HPA。
   - 重点区分 KEDA 管理的 0↔1 activation 与 HPA 执行的 1↔N scaling，并核对 HPA ownership transfer。
   - 长任务章节用于检查 scale-down，不把通用 queue worker 的保证直接套到 SSE inference。

2. [KEDA Prometheus Scaler](https://keda.sh/docs/latest/scalers/prometheus/)
   - 固定 query、server address/auth、threshold、activation threshold、namespace 和 empty/error 行为。
   - Query 必须产生一个可解释的标量；向量缺失、重复 series 或错误不能静默当成低负载。
   - 页面示例不替代 vLLM metric 的实际类型、labels 和单位校准。

3. [KEDA Releases](https://github.com/kedacore/keda/releases)
   - 选择实验实际版本，保存 operator/metrics server image digests、CRD schema 和 release notes。
   - `latest` 概念页用于导航；运行配置与生成 HPA 的行为必须绑定此处选定的 release。

4. [Kubernetes SIGs Prometheus Adapter](https://github.com/kubernetes-sigs/prometheus-adapter)
   - 固定 adapter release/image 与 discovery API，阅读 custom/external metrics 的 series、resource 和 query mapping。
   - 在运行 HPA 前，从 aggregated API 读取目标 metric，并与 Prometheus query 及 HPA `currentMetrics` 对账。
   - Adapter failure、empty result 和 stale data 的行为必须实测；不能假设 HPA 会直接查询 Prometheus。

## 兼容时选读

5. [KServe: Autoscaling LLMInferenceService with WVA](https://kserve.github.io/website/docs/model-serving/generative-inference/llmisvc/autoscaling/llmisvc-autoscaling)
   - 只在 Week 19 锁定版本支持时，核对 `spec.scaling`、`VariantAutoscaling`、`wva_desired_replicas` 与 HPA/KEDA actuator。
   - WVA 应是唯一 recommendation source，HPA 或 KEDA 二选一作 actuator；不要再叠加独立公式。
   - 当前页面示例 API version 可能与 overview 不同，必须以固定 CRD/admission 为准。

## 复用：只查本周增量问题

| 已有来源 | 本周只查什么 |
|---|---|
| [Week 15 references](week-15-references.md) #1–2 | Pod lifecycle、HPA ratio、missing metrics 与 stabilization；不重复列 HPA 新来源 |
| [Week 15 plan](week-15-plan.md) | Fixed/HPA baseline、冷启动时间线与 GPU-hours 方法；本周新增 KEDA 并重新冻结成本口径 |
| [Week 4 references](week-04-references.md) #7 | vLLM running/waiting/cache metrics 的原始类型、单位和 label |
| [Week 2 references](week-02-references.md) #2–3 | TTFT/TPOT、goodput 与 offered-request 失败分母 |
| [Week 19 references](week-19-references.md) #2/#4 | Fixed release 的 scaling/replicas schema 与实际存在的 scaling-related status conditions |
| [Week 18 references](week-18-references.md) #1–4 | llm-d Router/EPP 的 metric、readiness 与固定 release 边界 |

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 新增 1、3–4；复用 Week 15 #2、Week 19 #2 | Unique writer、adapter/APIService 与 scale target |
| Day 2 | 新增 2、4；复用 Week 4 #7 | PromQL、API mapping、单位、freshness 与缺失 contract |
| Day 3–4 | 新增 1、4；复用 Week 15 #1–2 | HPA/KEDA 响应与冷启动时间线 |
| Day 5 | 新增 1–2 | Outage、fallback、cooldown、idle/zero |
| Day 6 | 按需新增 5；复用 Week 18 #1–4 | WVA passthrough 与 observability smoke |
| Day 7 | 运行数据和成本清单 | 报告与 Week 21 handoff |

## 阅读后的自测问题

1. KEDA 的 0↔1 activation 与 1↔N scaling 分别由谁执行？
2. 为什么 `ScaledObject` 与一个独立 HPA 指向同一 target 会破坏 unique-writer contract？
3. Prometheus query 返回 empty vector、多个 series 或 stale 值时应如何处理？
4. 为什么 HPA 不能直接查询 Prometheus，adapter 的 mapping 和 aggregated API 如何进入证据链？
5. HPA desired replicas 增加后，为什么首个 token 仍可能迟到数分钟？
6. `idleReplicaCount` 或 min=0 能否单独证明 scale-to-zero 服务可用？
7. WVA + KEDA 中谁做 recommendation，为什么 actuator 不应再计算另一套需求公式？
8. Pod 数减少但节点仍保留时，allocated 与 billed 指标会怎样分离？
9. Grafana 中看到相同名字的曲线，为什么不代表 controller 用了完全相同的 query？
