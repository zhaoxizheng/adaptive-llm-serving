# Week 21 Reference Reading

第二十一周新增各云官方实现页面，用来建立“portable contract → provider adapter”映射。只在 GKE 实跑；Azure、Alibaba Cloud ACK 与 AWS 仅做官方文档研究，不把产品页面视为本地验证、跨云性能或采用率证据。

以下 URL 于 2026-09-22 只读核对。云功能、availability、版本和价格会变化；报告保存访问日期、实际 GKE 版本/feature state，并将未运行项标为 `officially-documented`。

## 新增必读：GKE Live Path

1. [Google Cloud: About GKE Inference Gateway powered by llm-d](https://cloud.google.com/kubernetes-engine/docs/concepts/about-gke-inference-gateway)
   - 阅读 GKE Gateway、managed inference extension/llm-d、supported routing 与 provider responsibility。
   - 不把 GKE Inference Gateway 笼统写成 Cloud Service Mesh，也不从产品概述推断所有 cluster mode/region 都可用。

2. [Google Cloud: Deploy GKE Inference Gateway](https://cloud.google.com/kubernetes-engine/docs/how-to/deploy-gke-inference-gateway)
   - 核对 prerequisites、cluster/GPU、GatewayClass、manifests、status 与 cleanup。
   - 教程机型/模型仅作示例；本周固定自己的可用版本、quota、model 和成本上限。

3. [Google Cloud: Serve with GKE Inference Gateway](https://cloud.google.com/kubernetes-engine/docs/how-to/serve-with-gke-inference-gateway)
   - 用于请求、routing 和 observation 的 live checklist；保存 selected endpoint/Pod 证据。
   - 示例成功请求不等于 SLO、HA 或性能结论。

4. [Google Cloud: Migrate GKE Inference Gateway](https://cloud.google.com/kubernetes-engine/docs/how-to/migrate-gke-inference-gateway)
   - 阅读 legacy alpha 到当前 API 的迁移边界，避免把旧 YAML 与新 CRD 混用。
   - 特别记录 GKE 路径中 `InferenceObjective` 的实际 `inference.networking.x-k8s.io` alpha group/version；它不是稳定 `InferencePool` v1 portability contract 的组成部分。
   - 只在实际集群需要时执行迁移；否则作为 version-skew 风险清单。

## 新增选读：其他云的官方 Mapping

5. [Azure AKS: AI Toolchain Operator](https://learn.microsoft.com/en-us/azure/aks/ai-toolchain-operator)
   - 将 KAITO 放在模型/GPU workload provisioning 层，不把它描述为 GAIE gateway。
   - 只记录当前官方文档范围和 prerequisites，未运行能力标为 `officially-documented`。

6. [Azure Application Gateway for Containers: Inference Gateway](https://learn.microsoft.com/en-us/azure/application-gateway/for-containers/how-to-inference-gateway)
   - 映射 Gateway API、InferencePool/EPP 与 Azure-managed data plane/identity。
   - 保存页面声明的 API/version/limitations；不据此宣称与 GKE implementation 等价。

7. [Alibaba Cloud ACK Gateway with Inference Extension](https://www.alibabacloud.com/help/en/ack/product-overview/ack-gateway-with-inference-extension)
   - 映射 Envoy Gateway/GAIE、smart routing 与 ACK control-plane responsibility。
   - 厂商扩展、安装渠道和 metric integration 单列，不包装成 upstream standard fields。

8. [AWS: Disaggregated Inference on AWS Powered by llm-d](https://aws.amazon.com/blogs/machine-learning/introducing-disaggregated-inference-on-aws-powered-by-llm-d/)
   - 了解 EKS 上 llm-d 的官方架构映射；博客是实现说明，不是稳定 API 或普遍 production adoption 证据。
   - 本周不部署 PD disaggregation，只抽取 gateway/router/workload/observability 角色。

9. [Amazon SageMaker HyperPod: Inference Gateway](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-model-deployment-inference-gateway.html)
   - 区分 GAIE-compatible concepts 与 HyperPod 专有资源/lifecycle。
   - `InferenceEndpointConfig` 等 AWS 对象不写成 upstream GAIE API，也不与 EKS portable path 混为一谈。

10. [GAIE: Gateway Implementations](https://gateway-api-inference-extension.sigs.k8s.io/implementations/gateways/)
    - 只把列表作为进一步核对 provider 文档的导航，不当作完整认证矩阵、采用率或统一 feature set。
    - 每个实现的 conformance、API version 和 production status 仍需分别验证。

## 复用：Portable Core

| 已有来源 | 本周只查什么 |
|---|---|
| [Week 16 references](week-16-references.md) #1–4 | Gateway API core v1 资源关系、HTTPRoute 与 weighted backend semantics |
| [Week 17 references](week-17-references.md) #1–6 | GAIE/InferencePool v1、fixed release 与 conformance scope |
| [Week 17 references](week-17-references.md) #7 | 固定 gateway 实现中的 ext_proc timeout/failure/stats |
| [Week 18 references](week-18-references.md) #1–4 | llm-d Router/EPP architecture、plugin pipeline 与 source pinning |
| [Week 19 references](week-19-references.md) #1–4 | KServe alpha LLMInferenceService 的声明、依赖和 status |
| [Week 20 references](week-20-references.md) #1–5 | KEDA/adapter/WVA unique-writer 与 metrics plumbing；只在 GKE 选定路径需要时复用 |

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 复用 Week 16–20 contract；新增 1 | Portable core 与 GKE adapter 边界 |
| Day 2 | 新增 2 | GKE preflight、部署与资源 status |
| Day 3 | 新增 3–4 | 请求验证与 API/version skew |
| Day 4 | GKE runtime 证据；复用 Week 17 #7 | 故障、恢复和实现行为 |
| Day 5 | 新增 5–7、10 | Azure/ACK mapping |
| Day 6 | 新增 8–10 | AWS mapping 与跨云差异表 |
| Day 7 | 来源快照、live results 和 gaps | 报告与 cleanup |

## 阅读后的自测问题

1. GKE Inference Gateway 与 Gateway API、GAIE、llm-d Router 分别是什么关系？
2. 为什么 GKE Inference Gateway 不应直接命名为 Cloud Service Mesh？
3. `InferencePool` API 稳定是否代表每个 provider extension 和托管产品也稳定？
4. KAITO 和 Azure inference gateway 为什么位于不同层？
5. ACK/AWS 的专有资源如何与 portable intent 分栏，而不是强行一一等价？
6. GAIE implementations 页面为什么不能证明采用率、完整 conformance 或 feature parity？
7. 哪些 GKE 证据属于 live-verified，哪些 Azure/ACK/AWS 结论只能写 officially-documented？
8. Render/diff 通过为什么仍不能宣称目标云可运行？
