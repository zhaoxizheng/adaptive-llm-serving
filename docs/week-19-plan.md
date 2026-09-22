# Week 19 Plan: KServe LLMInferenceService 声明式控制面与资源审计

> 时间预算：约 11 小时
>
> 本周主线：固定一个 KServe release，在独立集群实测当前仍属 alpha 的 `LLMInferenceService` API，追踪声明如何展开为 Gateway API、GAIE 与 workload 资源，并把所有权、状态和删除边界写成可审计 contract。
>
> 前置：[Week 15 plan](week-15-plan.md) 的 Kubernetes 双副本基线、[Week 18 plan](week-18-plan.md) 的 cache-aware 路由证据；阅读：[Week 19 references](week-19-references.md)。下列文件均为计划产出，不代表仓库已经实现或集群已经创建。

## 本周目标

1. 区分 KServe `LLMInferenceService`、`LLMInferenceServiceConfig` 与传统 `InferenceService` 的适用边界。
2. 固定 KServe、Kubernetes、Gateway API、GAIE、gateway provider、vLLM 与相关 CRD/controller 版本，拒绝跨版本拼接示例。
3. 以最小 vLLM 服务实测 defaulting、composition、owner references、生成资源、status conditions 与删除行为。
4. 明确用户声明、KServe controller、gateway provider/EPP 和底层 workload 各自拥有的字段。
5. 产出 Week 20 可使用的 replicas/metrics ownership map，不在本周启用动态扩缩容。

## 本周边界

- `LLMInferenceService` 当前按 alpha API 学习；即使在线页面出现 `v1alpha1` 与 `v1alpha2` 示例，也只以所选 release 实际安装的 CRD、schema、controller image 和 admission 行为为准。
- 本周做 declarative control-plane/resource audit，不做跨控制面的性能比较，也不把安装成功写成 production readiness。
- 只部署单节点、单模型、固定 `replicas` 的最小路径；不启用 WVA/KEDA、PD disaggregation、多节点 LWS、LoRA 或 KV offload。
- 使用一个已验证支持 GAIE 的 gateway provider；Gateway API 是规范、provider 是实现，不能把两者能力等同。
- CRD 和 cluster-scoped controller 影响不受 namespace 完全隔离；安装、升级和删除前先审查渲染结果与当前 kube context。
- 若固定 release 与现有 GPU 集群不兼容，可先做 CPU/control-plane reconcile 和 server stub；这只能证明资源行为，不能替代真实 GPU generation smoke。

## 本周最终产出

- `deploy/kserve/versions.lock.yaml`：计划记录 release、CRD source、controller/gateway/EPP/vLLM image digests 与 Kubernetes 版本。
- `deploy/kserve/base/`：计划保存最小 `LLMInferenceServiceConfig`、`LLMInferenceService` 及其依赖配置。
- `docs/llmisvc-resource-contract.md`：计划记录字段 ownership、composition/defaulting、生成资源 DAG、status 与清理边界。
- `scripts/audit_week19_llmisvc.sh`：计划实现 dry-run、apply、snapshot、mutate、delete 与 GPU smoke 的有界流程。
- `results/week19/`：计划保存 discovery/schema、对象 YAML、events、conditions、owner references 与 generation 证据。
- `reports/week19.md`：计划总结 alpha API 风险、实测资源图、版本兼容性和 Week 20 handoff。

## Fixed-release Compatibility Contract

Day 1 必须先冻结下表；网页 `latest` 只用于导航，不能成为运行版本。

| 层 | 必须记录的证据 |
|---|---|
| Kubernetes/GPU | Server version、node/GPU 型号、driver/device plugin、namespace 与 kube context |
| KServe | release/tag、controller image digest、installed/served/storage API versions、CRD schema checksum |
| Gateway | Gateway API CRDs、provider/controller release、GatewayClass 与 supported features/status |
| GAIE | release、`InferencePool` 等实际安装的 group/version/schema、EPP image digest |
| Runtime | vLLM image digest、model/revision、served model name、port、readiness 和资源 requests/limits |
| 可选依赖 | 只记录本次路径实际需要的 cert-manager/LWS 等；未启用组件明确标成 not installed |

若所选 KServe release 文档示例和 CRD 不一致，以 `kubectl explain`、discovery、导出的 CRD OpenAPI schema 和 controller behavior 为运行事实，并在报告中保留差异。不要自行把 `v1alpha1` manifest 改成 `v1alpha2` 后就声称兼容。

## Declarative Resource Audit

采用最小固定副本声明，依次保存 apply 前 server-side dry-run、apply 后对象快照、一次受控更新和删除结果。

| 输入/边界 | 需要验证的问题 |
|---|---|
| `LLMInferenceServiceConfig` + `baseRefs` | 合并顺序、用户 override、默认值和最终字段 provenance 是什么？ |
| `LLMInferenceService.spec.model/template/replicas` | 哪些字段直接进入 workload，哪些由 webhook/controller 转换？ |
| `spec.router` | 谁创建或引用 Gateway、HTTPRoute、InferencePool/EPP 相关对象？ |
| Deployment/Service/Pod | selector、port、model identity、resources、Ready 与 replica count 是否符合声明？ |
| Status/conditions | observedGeneration、Ready、子条件和原因能否关联到真实资源状态？ |
| Owner references/finalizers | 删除父对象后哪些资源级联，哪些 shared/externally managed 资源必须保留？ |

资源图必须基于实际 `api-resources`、对象 UID/owner references 和 events 绘制；文档图只作为待验证假设。对 managed 与 referenced 两种模式至少做 server-side dry-run 对照，但 GPU smoke 只跑一个最小主路径。

## Mutate 与 Failure Contract

1. 更新一个无害、可观察字段，确认 generation、reconcile、rollout 与 status 收敛，不直接手改生成对象制造“成功”。
2. 故意引用不存在或不兼容的 config/provider 对象，保存 admission/reconcile failure、condition reason 和 event；随后恢复有效声明。
3. 临时令一个后端不可 Ready，区分 Pod Ready、workload available、InferencePool endpoints 与 `LLMInferenceService` Ready。
4. 删除 `LLMInferenceService`，核对 namespaced child 的级联与 shared Gateway/config/CRD 的保留；不在共享集群删除 CRDs。
5. 若资源名称、kind 或 owner 链与文档不同，记录 release-specific 事实，不伪造预期对象。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 选择并锁定 KServe/GAIE/Gateway/provider 版本，导出 CRD 与 discovery 快照 |
| Day 2 | 1.5 h | 安装固定 CRDs/controller 与最小依赖，核对 discovery/schema/admission，再做 server-side dry-run |
| Day 3 | 2 h | Apply 最小声明，追踪 reconcile、生成资源、字段 provenance 与 owner references |
| Day 4 | 2 h | 验证 status/conditions、generation、model discovery 与 streaming generation smoke |
| Day 5 | 1.5 h | 做 managed/referenced、受控 mutate 和 invalid dependency 检查 |
| Day 6 | 1.5 h | 验证 NotReady 与删除边界，整理 resource DAG 和 replicas ownership map |
| Day 7 | 1 h | 完成 contract/报告、同步证据并停止 GPU/LB/磁盘计费资源 |

## 报告必须回答的问题

1. 实际安装的 `LLMInferenceService` group/version/schema 是什么，和阅读页面有哪些差异？
2. 一份最小声明最终生成或引用了哪些资源，每个对象由谁拥有和 reconcile？
3. Composition/defaulting 后的有效配置如何审计，用户字段能否追溯到来源？
4. `Ready=True/False` 汇总了哪些子条件，它是否与真实可生成 token 的时刻一致？
5. 直接编辑生成资源为什么会产生 drift，正确变更入口是什么？
6. 删除父对象后哪些资源消失、哪些保留，shared 与 cluster-scoped 资源的风险是什么？
7. Week 20 若引入 autoscaler，谁能写 replicas，哪些字段/资源必须从本周 fixed-replica contract 改变？

## 完成标准

- [ ] 运行证据绑定一个固定 KServe release、CRD schema、image digests 与兼容依赖版本。
- [ ] 报告明确标注 `LLMInferenceService` 为 alpha，未把网页示例版本当作集群事实。
- [ ] 最小声明的生成/引用资源、UID、owner references、conditions 与 events 已保存。
- [ ] Composition/defaulting 和字段 ownership 可追溯，未依赖手改生成资源。
- [ ] Generation/streaming smoke 与 control-plane Ready 时间分别记录；CPU stub 未冒充 GPU 性能证据。
- [ ] Mutate、无效依赖、NotReady 与删除行为均有证据或明确 blocker。
- [ ] Week 20 的 scale target、replicas writer 与 metric 接口已标出，但动态扩缩容尚未启用。
- [ ] 结果同步完成，实验 GPU、LoadBalancer、磁盘与临时资源已清理或列入保留清单。
