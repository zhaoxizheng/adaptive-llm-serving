# Week 19 中文代码导读：KServe LLMInferenceService 资源审计

本周沿着声明 → admission/defaulting → reconcile → 实际资源的方向审计 KServe alpha API。
对应 [学习计划](week-19-plan.md)、[参考资料](week-19-references.md)、
[运行手册](week-17-20-runbook.md)、[资源合同](llmisvc-resource-contract.md)。

## 1. 代码地图

| 文件 / 函数 | 作用 |
|---|---|
| [week19-resource-audit.yaml](../configs/week19-resource-audit.yaml) | managed/referenced、mutation、失败审计范围 |
| [versions.lock.yaml](../deploy/kserve/versions.lock.yaml) | KServe/CRD/runtime 版本和 artifacts |
| [base/README.md](../deploy/kserve/base/README.md) | 固定 release 声明文件的准备合同 |
| [platform_contract.py](../src/platform_contract.py) `crd_schema` | 从实际 served version 选择 schema |
| [run_platform_study.py](../scripts/run_platform_study.py) `snapshot/kserve_action` | 对象快照、父声明更新、删除与等待 |
| [analyze_platform.py](../src/analyze_platform.py) `resource_graph/deletion_audit` | UID owner 图与传递删除边界 |

## 2. 不猜测 alpha API version

`kserve_api_version` 初始为空，需要从固定 release 的 installed CRD 填写完整 group/version。
`kserve_crds` 保存对应官方 CRD，`schema_preflight()` 比较 installed 与 pinned OpenAPI
schema。网上某个 v1alpha1/v1alpha2 示例不构成兼容证据。

`render()` 直接读取两个经过校验和固定的声明输入：`kserve_managed` 与
`kserve_referenced`。每个文件包含实际 release 所需的 LLMInferenceServiceConfig、
LLMInferenceService 和相关 namespaced 配置。`--server-dry-run` 保存 admission 返回的
defaulted 对象；运行器不会把旧例子换个 apiVersion 就尝试部署。

两个变体都做 dry-run，GPU generation 只执行一个最小主路径。CPU stub 可以检查控制面，
但必须在报告标注它不代表 GPU generation 或性能。

## 3. `snapshot()` 收集什么

先调用 `api-resources`，只请求当前实际提供且列在 `audit_resources` 的 kinds。默认包括
Pod、Deployment、ReplicaSet、Service、EndpointSlice、Gateway、HTTPRoute、InferencePool、
LLMInferenceService/Config、HPA、ScaledObject、ConfigMap 和 Events。

输出每个对象的 UID、ownerReferences、managedFields、generation、conditions、finalizers，
并把未提供或读取失败的资源写入 `collection-errors.json`，不默认为空集合。release 若产生
额外 kind，先加入 `audit_resources` 再做完整快照；有 collection error 时不能宣称完整删除审计。

`objects.jsonl` 是一行一个原始 Kubernetes 对象。Secret 不在采样范围；任何 model/request
内容也不应放在需要导出的 ConfigMap 中。正式日志中的 prompt/body logging 需关闭。

## 4. 资源图为何以 UID 为键

`resource_graph()` 先给每个对象建立 UID 节点，再把 ownerReferences 转成边。
Kubernetes 名称可重用，但对象 UID 不会因名称相同而相同。一个典型的实际 owner 链可能是：

```text
LLMInferenceService UID -> Deployment UID -> ReplicaSet UID -> Pod UID
```

这里是解释 ownership 的例子，实际图完全从快照生成。Shared Gateway 若没有 parent owner
引用，不会被代码强行连到服务下面。快照外 owner 记录为 `external_owner_uids`，不会伪造节点。
`resources` CLI 同时输出 JSON 和 `.mmd` Mermaid 图，便于审查真实生成关系。

`managers.fields` 保留 field ownership 路径。它可以辅助定位谁声明 replicas、router 或
template；它本身不是 composition 合并顺序或持续写入行为的完整证明。还需要对照原始
Config/baseRefs 顺序、dry-run 返回值、effective declaration 和 controller behavior。

## 5. `kserve_action()` 只操作父声明

Mutate 输入 `kserve_mutation` 必须是一个相同名字的 LLMInferenceService，修改一个无害
spec 字段，例如 release 确认支持的 template annotation。运行器 apply 后等待父 generation
增加和当前 Ready=True。只改顶层 metadata annotation 可能不增加 generation，不能用来
完成这个实验。也不直接编辑生成 Deployment 来制造 reconcile 成功。

Delete 只删除配置的父 LLMInferenceService，`--wait=false` 后有界轮询父消失。
shared config、Gateway、CRD、controller 都不删除。父消失不代表所有子资源已清理完毕；
在 GC 收敛后另做一次 `--snapshot`，再运行删除分析。

`deletion_audit()` 根据 before owner 图递归计算所有 descendants，再与 after 的 UID
集合比较，分别输出仍存在的 owned、已消失的 owned、意外消失的 unowned。要求前后使用
相同 inventory 范围；对象缺失只能证明观测差异，不能单凭它证明删除由 GC 导致。

## 6. 执行顺序

```bash
bash scripts/audit_week19_llmisvc.sh --preflight
bash scripts/audit_week19_llmisvc.sh --server-dry-run --case managed
bash scripts/audit_week19_llmisvc.sh --server-dry-run --case referenced
bash scripts/audit_week19_llmisvc.sh --apply --case managed
bash scripts/audit_week19_llmisvc.sh --snapshot
bash scripts/audit_week19_llmisvc.sh --run --case managed
bash scripts/audit_week19_llmisvc.sh --mutate
bash scripts/audit_week19_llmisvc.sh --delete
bash scripts/audit_week19_llmisvc.sh --snapshot
python3.12 -m src.analyze_platform resources \
  --input results/week19/BEFORE/after/objects.jsonl \
  --output results/week19/resource-graph.json
python3.12 -m src.analyze_platform deletion \
  --input results/week19/BEFORE/after/objects.jsonl \
  --aux results/week19/AFTER/after/objects.jsonl \
  --parent-uid ACTUAL_PARENT_UID --output results/week19/deletion.json
```

`--run` 要求父 Ready 当前 generation，并通过配置 endpoint 发送 generation/streaming smoke。
必须在独立 namespace 确认该 endpoint 指向被审计父对象的实际 route；不要让旧 Week 18
HTTPRoute 继续接收请求。保存 `first_content_at` 与 Ready 的实际时间，不把两者当作同一事件。

invalid_dependency/not_ready 走 release-specific apply/restore artifacts；区分 admission
直接拒绝和 controller 接受后 Ready=False。前一种情况可能不会产生 runtime 请求阶段，
但运行 session 仍以 failed 保存原因和 before/after 证据。

## 7. 交给 Week 20 的内容

填写 [week19 报告](../reports/week19.md)，整理 replicas 字段、metric 接口和 owner 的实测图。
Week 20 默认回到 Week 18 的普通 `Deployment/vllm`，不把 KServe 生成 workload 与外部
HPA/KEDA 混用。`writer_check()` 会拒绝带 parent ownerReferences 的默认 scale target。

[分析测试](../tests/test_platform_analysis.py) 覆盖 owner 的多层传递、UID 区分和 shared
Gateway 保留；真实 admission、composition、Ready 和 finalizer 行为仍需集群验收。
