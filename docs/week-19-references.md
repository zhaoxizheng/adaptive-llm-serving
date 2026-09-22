# Week 19 Reference Reading

第十九周首次引入 KServe `LLMInferenceService` 的声明式资源模型；Gateway API Inference Extension（GAIE）直接复用 Week 17。当前 LLMInferenceService API 仍按 alpha 对待；阅读页面用于理解概念，运行事实必须来自一个固定 KServe/GAIE release 的 CRD、controller 与对象快照。

以下页面于 2026-09-22 只读核对。KServe 站点的默认文档会随 release 移动，且当前页面中可见 `v1alpha1`/`v1alpha2` 示例并存；执行时保存对应 release/tag、源码 permalink、CRD checksum 和 image digest。

## 新增必读

1. [KServe: Understanding LLMInferenceService](https://kserve.github.io/website/docs/model-serving/generative-inference/llmisvc/llmisvc-overview)
   - 区分 `LLMInferenceService`、传统 `InferenceService` 与底层 llm-d/vLLM 角色。
   - 只把 high-level resource graph 当作审计假设；“production-ready”等页面措辞不改变 alpha API 的兼容风险。
   - 固定 release 后从页面的版本化源码入口保存 permalink，不直接复制浮动 `latest` 镜像。

2. [KServe: LLMInferenceService Configuration](https://kserve.github.io/website/docs/model-serving/generative-inference/llmisvc/llmisvc-configuration)
   - 阅读 config composition、`baseRefs`、workload、router 和 managed/referenced resources。
   - 特别核对 `replicas`、`scaling` 的互斥和当前 CRD schema；示例 API version 不等于所选 release 的可用 version。

3. [KServe: LLMInferenceService Dependencies](https://kserve.github.io/website/docs/model-serving/generative-inference/llmisvc/llmisvc-dependencies)
   - 区分 Gateway API 规范、GAIE CRDs、gateway provider、EPP 与可选 LWS。
   - 依赖顺序和版本号只作为起点，安装前与固定 KServe release 的 compatibility evidence 对齐。

4. [KServe: LLMInferenceService Status Reference](https://kserve.github.io/website/docs/model-serving/generative-inference/llmisvc/llmisvc-status)
   - 对照 observed generation、顶层 Ready、workload/router 子条件和 reason/message。
   - Status 是 controller 视角；仍需以实际 endpoint 与 generation smoke 验证服务可用性。

## 固定 Release 的源码入口

5. [KServe Releases](https://github.com/kserve/kserve/releases)
   - 选择 controller/CRD release，保存 tag 与 assets/manifest 来源；不预设课程执行时仍应选择当前最新版本。
   - GAIE 的固定 release 入口复用 Week 17 #5；Gateway API core bundle 按固定 provider 的 compatibility declaration 选择并保存 checksum。

Release 页面是版本锁定入口，不增加通读任务。真正的执行合同由选定 tag 下的 manifest、CRD schema 和运行对象组成。

## 复用：只查本周增量问题

| 已有来源 | 本周只查什么 |
|---|---|
| [Week 15 references](week-15-references.md) #1 | Pod Ready、termination 与生成 workload 状态的区别 |
| [Week 16 references](week-16-references.md) #1–3 | Gateway API core v1 的资源角色、HTTPRoute status 与 matching contract |
| [Week 17 references](week-17-references.md) #1–5 | GAIE/InferencePool v1、固定 release 与 EPP 资源边界 |
| [Week 17 references](week-17-references.md) #7 | Envoy `ext_proc` 数据路径和 failure mode；不重复收录 URL |
| [Week 18 references](week-18-references.md) #1–4 | llm-d Router/EPP 的固定 release、plugin 与 KServe 底层实现边界 |
| [Week 18 references](week-18-references.md) #6 | Model/tokenizer/cache identity，只用于检查生成 runtime 参数 |

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 新增 1、3、5；复用 Week 17 #1–5 | 冻结版本与依赖矩阵 |
| Day 2 | 新增 2 | Composition、defaulting 与字段 provenance |
| Day 3 | 新增 3；复用 Week 17 #2–4/#7 | 资源 DAG、provider/EPP 边界 |
| Day 4 | 新增 4；复用 Week 15 #1 | Conditions 与真实 serving readiness |
| Day 5–6 | 新增 2、4；固定版本 schema/source | Mutate、失败与删除审计 |
| Day 7 | 运行对象和 contract | 报告与 Week 20 handoff |

## 阅读后的自测问题

1. `LLMInferenceServiceConfig` 与 `LLMInferenceService` 是何种 composition 关系，和 `ServingRuntime` 有何不同？
2. 为什么在线示例中的 `v1alpha1` 或 `v1alpha2` 不能直接证明集群支持该版本？
3. Gateway API、gateway provider、GAIE 与 EPP 分别负责什么？
4. 哪些资源由 KServe 创建，哪些可以引用外部对象，如何从 owner references 证明？
5. 顶层 `Ready=True` 是否足以证明首个 token 成功，为什么？
6. 为什么 namespace 不能完全隔离 CRD/controller 安装与删除风险？
7. 下一周启用 autoscaling 时，`spec.replicas`、scale subresource 与 actuator 的 ownership 如何变化？
