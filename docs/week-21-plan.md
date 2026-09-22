# Week 21 Plan: 云实现映射与可移植性验证

> 时间预算：约 11 小时
>
> 本周主线：以 vLLM + Gateway API core + GAIE/InferencePool + llm-d Router/EPP 为 portable baseline，只在 GKE 部署并运行一条受控路径；Azure、ACK 与 AWS 依据官方资料做能力/资源映射，不强制四云实跑，也不推断采用率或等价性。
>
> 前置：[Week 18 plan](week-18-plan.md) 的固定 llm-d 数据面、[Week 20 plan](week-20-plan.md) 的 autoscaling/observability contract；阅读：[Week 21 references](week-21-references.md)。下列文件均为计划产出，不代表任一云实现已部署、通过或具备相同 SLA。

## 本周目标

1. 定义 provider-neutral 的 vLLM workload、Gateway/HTTPRoute、InferencePool/EPP、identity 与 SLO contract；KServe/KEDA 作为可替换 control-plane adapters。
2. 在 GKE 固定集群/feature/release 前提下实跑 Inference Gateway 请求路径和最小 failure smoke。
3. 将 GKE 托管对象与 upstream Gateway API/GAIE/llm-d 对齐，明确 provider-added resources 与责任。
4. 从 Azure AKS/Application Gateway for Containers、Alibaba ACK、AWS EKS/HyperPod 官方资料建立同字段 mapping。
5. 区分可移植 API、实现能力、托管产品与厂商专有增强，不把“有文档”写成采用率、conformance 或运行证明。

## 本周边界

- 只有 GKE 是本周 live path；Azure、ACK、AWS 均为 desk mapping，不创建资源、不跑性能、不比较延迟或价格。
- GKE Inference Gateway powered by llm-d 是目标产品路径；不要将其泛称为 Cloud Service Mesh，外围 mesh 也不等于 inference routing implementation。
- Portable data plane 只包含 Kubernetes 上的 vLLM、Gateway API、GAIE 与明确版本的 llm-d Router/EPP contract；KServe alpha owner、HPA/KEDA scaler、provider CRD、IAM、LB、node provisioning 单列 control-plane/provider adapters。
- “官方支持/文档描述”只代表当前页面声称的范围；未亲自运行的云不能标为 verified、conformant、production-ready 或 feature parity。
- 不为追求跨云对称性部署四套 GPU 集群，不扩展到跨云流量、灾备或成本基准。
- 若 GKE 要求不同模型/GPU 或 GAIE API migration，先记录 deviation；不改写 Week 18/20 的历史 baseline。GKE 示例中的 `InferenceObjective` 等 provider/experimental API 必须按实际 group/version 单列，不能并入稳定的 `InferencePool` v1 portability claim。

## 本周最终产出

- `docs/portability-contract.md`：计划定义 portable core、provider adapters、capability/evidence states 与不可移植项。
- `docs/cloud-implementation-map.md`：计划保存 GKE/Azure/ACK/AWS 的官方资源、controller、routing、scaling、identity、observability 映射。
- `deploy/gke/`：计划保存固定 GKE cluster/features、Gateway/InferencePool、实际使用的 `InferenceObjective` 等 provider resources、model workload 与安全配置。
- `configs/week21-gke.yaml`：计划冻结模型、负载、路由、autoscaling 模式、配额和运行上限。
- `scripts/run_week21_gke_portability.sh`：计划执行 preflight、apply、request attribution、failure smoke 和 cleanup。
- `results/week21/` 与 `reports/week21.md`：计划保存 GKE live evidence、跨云 desk matrix、gaps 与不可比较声明。

## Portable Contract 与 Evidence Levels

```text
Client
  → Gateway API Gateway / HTTPRoute
  → GAIE InferencePool + llm-d Router/EPP
  → vLLM model-serving Pods
  ↕ Prometheus-compatible metrics
  → provider-neutral metrics/SLO evidence

Optional control-plane adapters: KServe LLMInferenceService (alpha), HPA/KEDA/WVA scaling path
Provider adapters: GatewayClass/LB, IAM/identity, GPU nodes, storage, metrics plumbing
```

| Evidence state | 允许写出的结论 |
|---|---|
| `live-verified` | 本周在固定 GKE 版本实际部署、观察 status 并发出请求 |
| `officially-documented` | 厂商官方页面描述该能力，但本周未运行 |
| `mapping-inferred` | 根据资源角色做的迁移假设，必须列验证步骤 |
| `unknown/not documented` | 当前证据不足，不能用其他云行为填空 |

每一格都保存 official source URL、access date、页面所述 API/version/status 和 evidence state；`mapping-inferred` 也必须指向其推断所依据的官方来源并明确未验证。不得统计“几家云支持”来代表市场采用，也不得把 GAIE implementations 列表当完整厂商认证。

## GKE Live Validation

1. 固定 GKE mode/version/location、GatewayClass、Inference Gateway feature/API、GAIE resources、llm-d artifacts、vLLM image/model 和 GPU node pool；若使用 `InferenceObjective`，保存其实际 `inference.networking.x-k8s.io` alpha group/version/schema。
2. 部署最小单模型路径，保存 Gateway/HTTPRoute/InferencePool 以及 provider/experimental `InferenceObjective` 的 status、EPP/Pod identity、endpoint membership 和 provider-created resources。
3. 发出 non-streaming 与 streaming 请求，用 request/run ID 证明 Gateway → EPP decision → selected vLLM Pod；记录 TTFT/TPOT 只作运行 smoke，不做跨云性能结论。
4. 使一个 backend NotReady，验证 endpoint removal、新请求和既有 stream；短暂停止 EPP 时记录固定实现的 failure mode 与恢复。
5. 若沿用 Week 20 autoscaling，只选一个 writer 做短 burst smoke；否则固定副本。不能在本周重新调 threshold 或声称云间成本优势。
6. 清理前保存 provider billing/resource inventory；删除实验资源后复查 GPU node、LB、disk/IP 和 cluster 是否仍计费。

## Cross-cloud Mapping Matrix

| Portable concern | GKE（live） | Azure（desk） | ACK（desk） | AWS（desk） |
|---|---|---|---|---|
| Gateway implementation | GKE Inference Gateway / GKE Gateway | Application Gateway for Containers inference gateway | ACK Gateway with Inference Extension | EKS llm-d reference path；HyperPod proprietary path 另列 |
| Inference backend API | 实测稳定 `InferencePool` v1；`InferenceObjective` alpha 另列 | 官方页所述 InferencePool/EPP | 官方页所述 GAIE resources | EKS 的标准 API mapping 与 HyperPod `InferenceEndpointConfig` 分栏 |
| Model workload | GKE deployment example 的实际 object | AKS/KAITO 只作为 workload provisioning mapping | Cloud Native AI Suite/vLLM mapping | EKS vLLM 或 HyperPod endpoint mapping |
| Routing extension | 实际 llm-d Router/EPP artifact | 官方 provider/EPP description | 官方 smart routing/provider description | 官方 llm-d/HyperPod description |
| Autoscaling | fixed 或 Week 20 单 writer | 只记录文档机制和待验证项 | 只记录文档机制和待验证项 | 只记录 EKS/HyperPod 文档机制和待验证项 |
| Identity/observability | 实测 IAM/service account、metrics/logs | 官方 auth/metrics mapping | 官方 RAM/metrics mapping | 官方 IAM/CloudWatch/Prometheus mapping |

表中名称相似不代表 schema、lifecycle、failure mode、billing 或 conformance 相同。KAITO 是 AKS 模型/GPU workload 工具链，不等同 GAIE gateway；HyperPod 专有 CRD 不写成标准 GAIE API；ACK/GKE 的托管控制器也不由 upstream API 自动保证。

## Portability Delta Test

将 GKE live manifests 分为三层并做一次 render/diff：

- `portable/`：模型 image/args、Service-like labels、Gateway/HTTPRoute/InferencePool intent、SLO/metric contract。
- `providers/gke/`：GatewayClass、`InferenceObjective` 等 alpha/provider resources、LB annotations/policies、IAM、GPU node selector、storage 和 feature enablement。
- `providers/{azure,ack,aws}/mapping.md`：官方对象映射、缺口和未来 live-validation checklist，不创建伪 manifests 冒充可运行。

统计变化文件/字段只能描述配置迁移表面积；没有在目标云 apply/status/request 验证，就不能声称“可移植成功”。KServe `LLMInferenceService` 仍为 alpha optional owner，不将其作为四云稳定统一 API。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 冻结 portable contract、evidence states 与 GKE 版本/配额/成本上限 |
| Day 2 | 2 h | 在 GKE 部署 Inference Gateway、InferencePool/EPP 与最小 vLLM workload |
| Day 3 | 2 h | 验证 status、request-to-Pod attribution、streaming 与指标链 |
| Day 4 | 1.5 h | 做 backend NotReady、EPP interruption 和恢复 smoke，保存计费/资源清单 |
| Day 5 | 1.5 h | 阅读并填写 Azure 与 ACK 官方 mapping、unknowns 和验证清单 |
| Day 6 | 1.5 h | 阅读并填写 AWS mapping，完成 portable/provider render diff |
| Day 7 | 1 h | 完成报告、清理 GKE 资源并复查残留计费 |

## 报告必须回答的问题

1. Portable core 的 API/组件/版本是什么，哪些字段已被 GKE live evidence 验证？
2. GKE 托管路径创建和拥有了哪些资源，`InferenceObjective` 的实际 alpha schema 与稳定 `InferencePool` v1 如何分层？
3. Backend NotReady 与 EPP 故障时，新请求、已有 stream 和恢复行为分别如何？
4. GKE manifest 中哪些字段属于 provider adapter，移除它们后还剩下什么 portable intent？
5. Azure、ACK、AWS 每项 mapping 的证据是 official documentation、inference 还是 unknown？
6. 哪些同名概念实际不是等价 API，例如 KAITO、HyperPod resources 与 GAIE resources？
7. 本周哪些结论不能覆盖性能、成本、SLA、adoption、conformance 或四云可运行性？

## 完成标准

- [ ] Portable data plane、可选 control-plane adapters 与 GKE adapter 分层，所有版本、images、features 和 API schemas 可追溯。
- [ ] GKE 路径有 resource status、request attribution、streaming、backend/EPP failure 与恢复证据。
- [ ] GKE smoke 未被夸大为跨云性能或生产结论。
- [ ] Azure、ACK、AWS mapping 每格都附官方来源和 evidence state，未强制部署。
- [ ] 未把文档存在、实现列表或营销措辞推断为采用率、feature parity、SLA 或 conformance。
- [ ] KServe alpha API、GAIE `InferencePool` v1、GKE `InferenceObjective` alpha 与其他 provider extensions 的成熟度分开表达。
- [ ] 迁移 gaps、未来验证命令和不可比较项齐全，未创建未经验证的“可运行”云 manifests。
- [ ] GKE 结果已同步，GPU/LB/disk/IP/node/cluster 残余计费已检查。
