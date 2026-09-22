# Week 25 Reference Reading

第二十五周围绕 KubeRay/RayService 生命周期、Ray Serve LLM 和 placement groups 阅读。重点是区分 Kubernetes admission/Pod placement 与 Ray runtime scheduling，不把两个 gang 概念合并。

以下官方页面已于 2026-09-22 核对。Ray 文档的 `latest` 与 `master` 可能描述不同版本；执行前必须用固定 Ray/KubeRay release、CRD schema、image digest 和实际 API 验证示例。

## 新增必读：KubeRay 与 RayService

1. [Getting Started with KubeRay](https://docs.ray.io/en/latest/cluster/kubernetes/getting-started.html)
   - 区分 RayCluster、RayJob 与 RayService，理解 operator 如何创建 head/worker Pods。
   - 本周先固定 cluster 与 replicas；不因页面同时展示 autoscaling 就启用它。

2. [Deploy Ray Serve Applications with RayService](https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/rayservice.html)
   - 阅读 RayService reconciliation、Serve configuration、upgrade、status 和 service exposure。
   - 分开验证修改 `serveConfigV2` 的 application update 与修改 `rayClusterConfig` 的 cluster upgrade/replacement，不合并时间线或 rollback 结论。
   - 只有运行 Ray Serve application 时才使用 RayService，不把它当作通用多 Pod workload API。

## 新增必读：Serve LLM 与 Runtime Placement

3. [Ray Serve LLM Architecture](https://docs.ray.io/en/latest/serve/llm/architecture/overview.html)
   - 追踪 Serve deployment/replica、router/proxy、vLLM engine 和 worker actors。
   - 将请求路由、replica lifecycle 与 vLLM 的 TP/PP collective 分层记录。

4. [Ray Serve LLM Cross-Node Parallelism](https://docs.ray.io/en/master/serve/llm/user-guides/cross-node-parallelism.html)
   - 读取跨节点 engine、placement configuration 和硬件需求；只移植固定版本存在的 API。
   - `master` 页面是移动目标，不能直接当作已安装 stable release 的能力证明。

5. [Ray Placement Groups](https://docs.ray.io/en/latest/ray-core/scheduling/placement-group.html)
   - 阅读 bundles、ready/pending、lifecycle、resource accounting 及 PACK/SPREAD/strict strategies。
   - PG 在已运行 Ray cluster 内原子预留 runtime resources；它不创建 Kubernetes nodes/Pods，也不替代 Kueue admission。

6. [KubeRay with Kueue](https://docs.ray.io/en/latest/cluster/kubernetes/k8s-ecosystem/kueue.html)
   - 阅读 Ray workload 如何进入 Kueue，以及 admission 对 cluster Pods 的影响。
   - 保存 Kueue condition 与 Ray/PG state 的关联时间线，而不是把它们压缩成一个 gang-ready 标志。

## 复用：只查本周增量问题

| 已有来源 | 本周只查什么 |
|---|---|
| [Week 23 references](week-23-references.md)；[Week 14 references](week-14-references.md) #1 | 固定 vLLM TP/PP/runtime 和输出 contract，不把 Serve/KubeRay 差异归因到 parallelism |
| [Week 24 references](week-24-references.md) #5–7 | 复用相同 queue/quota/TAS 与 admission 口径，和 Ray PG 分层对照 |
| [Week 15 references](week-15-references.md) #1 | Pod readiness/termination 与 endpoint 生命周期 |
| [Week 22 references](week-22-references.md) #1–2 | 请求失败分母、重复与不确定性，不从恢复流量计算稳态收益 |

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 1–2 | CR ownership 与两层状态机 |
| Day 2 | 1；复用 Week 23 | 固定 RayCluster 与 resource inventory |
| Day 3 | 4–5 | Cross-node engine 与 placement groups |
| Day 4 | 2–4 | RayService/Serve LLM 请求与生命周期 |
| Day 5 | 6；复用 Week 24 #5–7 | Kueue admission 与 PG timeline |
| Day 6 | 2、5 | 故障、升级、rollback 与资源释放 |
| Day 7 | 回看 1–6 和实验状态 | Week 26 ADR handoff |

## 阅读后的自测问题

1. RayCluster、RayJob 与 RayService 分别适合什么生命周期？
2. KubeRay operator、Kubernetes scheduler 与 Ray scheduler 分别调度什么对象？
3. Placement group ready 能否证明 Kubernetes workload 已经被 Kueue 准入，为什么？
4. Kueue all-or-nothing admission 能否保证 Ray actors 按期望 bundles 启动，为什么？
5. PACK、SPREAD 与 strict strategies 在资源不可满足时有什么差异？
6. 为什么 RayService 的 route/upgrade 行为不能归因给 vLLM TP/PP？
7. `serveConfigV2` application update 与 `rayClusterConfig` cluster upgrade 为什么必须分开评价？
