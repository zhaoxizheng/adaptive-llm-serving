# LLMInferenceService 资源审计合同

本周 API 按 alpha 对待。运行版本来自固定 KServe release 的 installed/served/storage
CRD schema；参见 [代码导读](week-19-code-walkthrough.md) 和
[版本锁](../deploy/kserve/versions.lock.yaml)。

| 对象/字段 | 预期入口 | 验证证据 |
|---|---|---|
| Config/baseRefs | 用户声明 | 原始文件、引用顺序、admitted/defaulted spec |
| LLMInferenceService model/template/replicas | 用户声明 | generation、managedFields、controller reconcile |
| Workload、Service、Pool、Route、EPP | 固定 release 的 controller/provider | 实际 UID ownerReferences、events、effective fields |
| 外部 Gateway/config | 引用对象的原管理者 | 无级联 owner、删除前后相同 UID |
| Ready | controller 汇总状态 | observedGeneration 与真实 first-token 独立时间 |

表中的所有权是待验证问题，不能代替实际资源图。`resource_graph()` 仅根据对象 UID
与 ownerReferences 建边；composition provenance 还需原始 baseRefs 与 defaulting 差异。

变更父声明而非生成对象；mutation 应令 spec generation 增加。无效 dependency 的 admission
拒绝与 reconcile 失败分开。NotReady 时分别观察 Pod、Deployment、Pool 和顶层 Ready。

Delete 仅删除实验父 LLMInferenceService。等待 GC 后，以同范围完整 before/after inventory
核查 descendants 和 shared objects。Collection error、缺少 kind 或遗漏 cluster-scoped
依赖时不能声称全面清理；CRD/controller 不由实验脚本删除。

Week 20 默认 target 为不受 KServe parent 管理的普通 vLLM Deployment。不能把 alpha
controller 的 fixed replicas 与 HPA/KEDA 同时交给多个 writer。
