# Week 22 Reference Reading

第二十二周新增 rollout、timeout/mirroring、SLO alerting 与 incident runbook 资料；Google SLO implementation 和 SciPy bootstrap 从旧 Week 19 迁到本周，仍作为首次编号来源。Gateway API、GAIE、llm-d、KServe/KEDA 的架构资料只复用前周，不重复编号。

以下页面于 2026-09-22 只读核对。运行时仍以固定 controller/release 的 supported features、CRD schema 和 status 为准；通用指南不自动构成当前实现保证。

## 新增必读：Evaluation 与 SLO

1. [Google SRE Workbook: Implementing SLOs](https://sre.google/workbook/implementing-slos/)
   - 只读 What to Measure、Moving from SLI Specification to SLI Implementation 与 Documenting the SLO。
   - 将 TTFT/TPOT、成功、超时和 offered requests 写成可审计 numerator/denominator/window，不用通用 HTTP 示例替代 streaming 语义。

2. [SciPy `scipy.stats.bootstrap`](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html)
   - 阅读 `paired`、`statistic`、`confidence_level`、`rng`、degenerate sample warning 和示例。
   - 以独立 run 或预定义 block 为重采样单位；同一 burst 的相关请求不是自动独立，API 也不会替你做时间序列 block bootstrap。
   - 少量 repeats 只能表达有限波动；增加 resamples 不会增加真实信息，也不能制造稳定 P99 区间。

3. [Google SRE Workbook: Alerting on SLOs](https://sre.google/workbook/alerting-on-slos/)
   - 阅读 error-budget burn、multi-window/multi-burn-rate 与 alert precision/recall。
   - 本项目流量规模较小，先验证 query 与事件关联；不直接照抄生产阈值并宣称 paging policy 完成。

## 新增必读：Rollout 与 Failure

4. [Kubernetes Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)
   - 聚焦 rollout status、revision history、progress deadline、pause/resume 与 rollback。
   - KServe/其他 controller 生成的 workload 必须从其 owner 入口变更；直接 rollback child Deployment 可能被 reconcile 覆盖。

5. [Kubernetes Disruptions and Pod Disruption Budgets](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)
   - 区分 voluntary/involuntary disruptions、eviction 与 direct deletion。
   - PDB 不能防止所有故障或删除，也不替代 readiness、drain、capacity 与 rollout 验证。

6. [Gateway API: HTTP Request Mirroring](https://gateway-api.sigs.k8s.io/guides/http-request-mirroring/)
   - 只在固定 gateway implementation 支持时使用；核对 mirrored request、response discard 与 status。
   - LLM 请求可能有成本、敏感数据和非幂等副作用；本周只允许合成、无副作用 payload。

7. [Gateway API: HTTP Timeouts](https://gateway-api.sigs.k8s.io/guides/http-timeouts/)
   - 区分 request 与 backend request timeout，并与 client/SSE/controller deadline 对齐。
   - 字段被 API 接受不代表 provider 已支持；以 Route conditions 和 runtime timeout smoke 验证。

8. [Google SRE Workbook: Canarying Releases](https://sre.google/workbook/canarying-releases/)
   - 阅读 canary population、evaluation 与 rollback 的原则，用来预设门槛而非事后挑指标。
   - 通用发布建议映射到 LLM streaming、模型加载、cache warmup 和 weighted upstream attempts 时需保留差异。

9. [Google SRE Workbook: Incident Response](https://sre.google/workbook/incident-response/)
   - 提取 roles、communication、mitigation、recovery validation 与 postmortem handoff。
   - 实验 runbook 不冒充完整组织流程，但必须给出 owner/escalation 和证据包。

## 按需新增：固定实现的 Day-2 细节

10. [llm-d Router Operations](https://llm-d.ai/docs/operations/router)
    - 按固定 llm-d release 查 HA、resource sizing、failure/telemetry 和 operational checks。
    - 页面是实现运维补充；router architecture、plugin 与 EPP contract 已在 Week 18 首次收录。

11. [KServe: LLMInferenceService Canary Rollout](https://kserve.github.io/website/docs/model-serving/generative-inference/llmisvc/canary-rollout)
    - 只在 Week 19 固定 release 支持时，核对 group/weight、controller-managed routes、status 与删除行为。
    - KServe `LLMInferenceService` 仍是 alpha；教程不替代本周 schema/runtime 验证。

## 复用：只查 Capstone 增量问题

| 已有来源 | 本周只查什么 |
|---|---|
| [Week 2 references](week-02-references.md) #1–3 | TTFT/TPOT/goodput 定义与 workload-aware SLO，不再学习推理阶段 |
| [Week 16 references](week-16-references.md) #4 | Weighted backend 的相对权重与 upstream attempt 解释 |
| [Week 17 references](week-17-references.md) #3/#6/#7 | InferencePool failure mode、conformance 边界与 ext_proc timeout/stats |
| [Week 18 references](week-18-references.md) #1–6 | llm-d routing、prefix/load tradeoff、cache identity 与固定源码 |
| [Week 19 references](week-19-references.md) #1–4 | KServe alpha owner、configuration/status 与生成资源回滚边界 |
| [Week 20 references](week-20-references.md) #1–5 | KEDA/adapter/WVA writer、fallback、cooldown 与 metric outage |
| [Week 21 references](week-21-references.md) #1–4 | GKE live implementation、provider status 与 API migration 边界 |

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 新增 1–3；复用 Week 2 #1–3 | 冻结 SLO、held-out 与统计 contract |
| Day 2–3 | 新增 2；复用 Week 16–21 | 最终矩阵、独立重复与分析 |
| Day 4 | 新增 5、7、10；复用 Week 17 #7 | Pod/EPP/metric 故障与恢复 |
| Day 5 | 新增 4、6、8、11 | Mirror/canary/promotion/rollback |
| Day 6 | 新增 3、9；运行 evidence | Alerts 与 runbook tabletop |
| Day 7 | 全部 contracts、原始数据和成本清单 | 最终报告与 cleanup |

## 阅读后的自测问题

1. Offered-request SLO attainment 与仅成功请求 latency quantile 的分母有何不同？
2. 为什么增加 bootstrap resamples 不能弥补只有三个独立 runs？
3. Held-out 数据何时可以揭盲，修 bug 后哪些 cells 必须重跑？
4. PDB 能保护哪些 voluntary disruptions，哪些 Pod/节点故障不受它保护？
5. Request、backend request、client 与 rollout timeout 如何避免互相矛盾？
6. Mirror 为什么不能替代 canary，LLM 请求镜像有哪些成本、隐私和副作用风险？
7. Weighted canary 为什么要核对 upstream attempts，而不能要求少量客户端请求精确命中权重？
8. 回滚 child Deployment 为什么可能被 KServe reconcile 覆盖？
9. HTTP 200、Pod Ready 或副本恢复为什么都不足以单独证明 incident 已恢复？
10. 哪些最终结论只适用于固定 GKE/release，不能外推到 Azure、ACK 或 AWS？
