# Week 25 Plan: Ray + KubeRay：RayService 生命周期与 Placement Group

> 时间预算：约 11 小时
>
> 本周主线：在固定 KubeRay 集群中运行 Week 23 的跨节点 vLLM engine，再用 RayService 验证 Ray Serve 应用生命周期；同时把 Kueue workload admission、Kubernetes Pod scheduling 与 Ray placement group 的 runtime bundle 预留拆成不同状态机。
>
> 前置：[Week 23 plan](week-23-plan.md) 的 runtime contract 和 [Week 24 plan](week-24-plan.md) 的 Kueue/admission 时间线；阅读：[Week 25 references](week-25-references.md)。下列文件是待完成产出，不代表仓库已有实现。

## 本周目标

1. 区分 `RayCluster`、`RayJob`、`RayService`，只在真正运行 Ray Serve 时使用 `RayService`。
2. 追踪 RayService → Serve deployment/replica → vLLM engine → worker actor → GPU 的完整路径。
3. 验证 placement group bundles、strategy、pending 原因、actor placement 与资源释放。
4. 证明 Kueue workload admission 与 Ray placement group readiness 是不同层、不同资源视图。
5. 验证 worker/head 故障，并分开记录 Serve application update 与 Ray cluster upgrade，为 Week 26 ADR 留下证据。

## 本周边界

- KubeRay 将 Ray CRs reconcile 为 head/worker Pods，并管理 Ray cluster/application 生命周期；Kubernetes scheduler 仍负责 Pod-to-node placement。
- Ray scheduler 只在已经运行的 Ray cluster 内调度 tasks/actors；placement group 原子预留 Ray logical-resource bundles，不能准入 Kubernetes workload、创建节点或保证 Pending Pods 获得资源。
- Kueue 在 Kubernetes 层做 queue/quota 和 workload-level topology admission；它不执行 Ray actor，Pod placement 仍由 Kubernetes scheduler 完成。反过来，Ray placement group 不替代 Kueue 的配额、优先级或 workload admission。
- 主矩阵固定 head/worker Pods、Serve replicas 和 node pool，关闭 Ray、Serve、Kubernetes autoscaling；伸缩只记录 ownership，不做联合性能实验。
- 固定 Ray、KubeRay operator/CRDs、RayService spec、vLLM、model/image revision 与 workload；实际 schema 和镜像必须匹配。
- RayService 的两类更新分开测试：修改 `serveConfigV2` 的 Serve application update 是 C6a；修改 `rayClusterConfig` 并触发新 Ray cluster/replacement 的 cluster upgrade 是 C6b。两者使用独立时间线、可用性与 rollback 证据。
- `2 nodes × 1 L4` 只支持功能、调度和恢复 smoke；本周不以普通 TCP 数据评价跨节点推理性能。
- 不把 LWS 加入本周运行矩阵，不把 Ray Serve 的路由/生命周期差异归因给 vLLM TP/PP。

## 本周最终产出

- `docs/kuberay-runtime-contract.md`：KubeRay、Kueue、Kubernetes、Ray scheduler 与 Serve ownership。
- `configs/week25-kuberay/`：固定版本 RayCluster、RayService、Serve/vLLM、placement group 与 Kueue manifests。
- `scripts/run_week25_kuberay.sh`：部署、状态采集、infeasible PG、Kueue admission、故障/升级和清理。
- `results/week25/`：CR conditions、Pod/actor/bundle mapping、阶段时间线、请求结果和资源释放证据。
- `reports/week25.md`：runtime 分层、matrix、失败与 Week 26 ADR handoff。

## 两层调度与生命周期 Contract

| 层次 | 决策对象 | Ready/成功证据 | Pending 不代表什么 |
|---|---|---|---|
| Kueue | 整个 Ray workload 的 quota、queue 与 admission | Workload admitted、quota reservation 可见 | 未准入不等于 Ray placement strategy 错误 |
| Kubernetes/KubeRay | Head/worker Pods、Service 和 Ray CR lifecycle | 所有需要的 Pods scheduled/Ready，Ray CR condition 正常 | Pods Ready 不等于 Serve/vLLM engine 已就绪 |
| Ray placement group | Ray 集群内的一组 logical-resource bundles | PG ready，bundle-to-node mapping 与 actor resources 匹配 | PG pending 不等于 Kubernetes 需要重新准入 |
| Ray Serve/vLLM | Deployment/replica、engine workers、API request | Serve status healthy，完整 world size，生成/streaming smoke 通过 | Actor 存活不等于 endpoint 已可安全接流量 |

每次运行按 `Kueue admitted → Pods scheduled → Ray cluster ready → PG ready → actors joined → model loaded → Serve healthy → request ready` 保存时间线。若某阶段不适用，也要显式标记，而不是合并成总启动时间。

## 实验矩阵

| Cell | 场景 | 验证点 |
|---|---|---|
| C0 | 固定 RayCluster：1 head + 2 GPU workers | CR/Pod/Ray node resources 一致，运行最小远程 task 与 GPU smoke |
| C1 | Ray backend 运行 Week 23 固定跨节点 vLLM config | Engine worker actor、rank、PG bundle 与物理 GPU 映射；输出正确 |
| C2 | RayService + Ray Serve LLM | OpenAI endpoint、streaming、health/readiness 与完整 request trace |
| C3 | 可行 PG strategy + 一个使用 `STRICT_PACK` 或 `STRICT_SPREAD` 的故意不可行 placement group | Ready/pending 原因、无部分 actor 泄漏、删除后 bundles/resources 释放 |
| C4 | 相同 Kueue queue/quota 下整集群 admission | Kueue 与 PG 两层状态串联，分别报告 admission wait 和 runtime wait |
| C5 | 删除 GPU worker、删除 head | 请求影响、actor/cluster 重建、endpoint 恢复与旧资源清理 |
| C6a | 只修改一个冻结的 `serveConfigV2` application field | Serve application reconcile/update、可用性窗口和 rollback；`rayClusterConfig` 不变 |
| C6b | 只修改一个冻结的 `rayClusterConfig` image/Pod field | 新 Ray cluster/replacement、service switch、旧集群回收和 rollback；`serveConfigV2` 不变 |

PG strategy 必须由硬件假设驱动。分别记录 `PACK`、`SPREAD`、`STRICT_PACK` 或 `STRICT_SPREAD`；使用 strict strategy 且不可满足的 placement group 应保持 pending 并提供原因，不能静默降级或临时缩小 bundle。

## 测量与故障验收

- 保存 Ray/KubeRay/CRD/image/Serve/vLLM revisions、Ray logical resources、Pod requests/limits、node labels 和实际 actor mapping。
- PG bundles 必须覆盖 distributed replica 的 CPU/GPU actor 需求；通过 PG ready 后仍核对 child tasks/actors 是否落在预期资源组。
- 分开报告 Kueue admission、Pod scheduling/image pull、Ray bootstrap、PG wait、model load、Serve readiness 和 steady state。
- 请求侧保存 TTFT、TPOT、goodput、错误/取消和 route/replica attribution；它们用于功能回归，不从普通 TCP 小样本产生平台性能结论。
- Worker/head 故障期间的错误、重试和冷启动不计入 steady-state throughput，但必须进入 SLO/availability 与恢复报告。
- 删除 infeasible PG、RayService 和 RayCluster 后检查 actor、PG、Pod、Service、GPU reservation 和云资源均收敛。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 固定 Ray/KubeRay 版本，画出 RayCluster/RayService 与两层调度状态机 |
| Day 2 | 1.5 h | 部署 C0，核对 head/worker Pods、Ray nodes 与 GPU logical resources |
| Day 3 | 2 h | 运行 C1/C3，验证 vLLM actors、PG bundles 与可行/不可行 placement |
| Day 4 | 2 h | 运行 C2/C6a，验证 endpoint/streaming，并单独记录 Serve application update |
| Day 5 | 1.5 h | 运行 C4/C6b，关联 admission/PG timeline，并单独记录 Ray cluster upgrade |
| Day 6 | 1.5 h | 运行 C5，完成 worker/head failure 与两类更新的 rollback/清理核对 |
| Day 7 | 1 h | 整理 ownership、失败、资源释放和 Week 26 公平比较 contract |

## 报告必须回答的问题

1. 为什么这个 workload 使用 RayService，而不是只用 RayCluster 或把它当通用多 Pod launcher？
2. 一次请求如何从 RayService 追踪到 Serve replica、vLLM engine、worker actor 和 GPU？
3. Kueue admission、Kubernetes scheduling 与 Ray PG readiness 各自使用什么资源视图？
4. 可行和不可行 placement group 的 bundle、strategy、pending 与清理行为是什么？
5. 哪个阶段主导冷启动，哪些时间不能计入 steady-state serving？
6. Worker/head 故障、`serveConfigV2` application update 与 `rayClusterConfig` cluster upgrade 各自如何影响请求、actor、endpoint 和旧资源？
7. Week 26 必须固定哪些 runtime/serving 差异，哪些差异只能作为整套栈的 ADR 取舍？

## 完成标准

- [ ] C0–C6 均有 manifests、conditions/events、Pod/Ray node/actor/bundle mapping 和明确状态。
- [ ] 能完整追踪 RayService → Serve → vLLM → worker actor → GPU，并验证一次 streaming request。
- [ ] Kueue admission 与 Ray PG readiness 作为两层状态机记录，没有互相替代。
- [ ] 一个可行 PG 和一个使用 strict strategy 的故意不可行 PG 均已验证，未留下部分 actor 或资源泄漏。
- [ ] Worker/head 故障至少各一次；C6a application update 与 C6b cluster upgrade 分别完成并各有 rollback/清理证据。
- [ ] 所有 autoscaler 在主矩阵中关闭，冷启动/恢复流量未混入 steady-state 指标。
- [ ] 普通 TCP/L4 数据只标为功能 smoke，没有归因 LWS/KubeRay 或宣称生产性能。
- [ ] 结果同步并绑定 Git commit/image/CRD versions，PG、Pods、节点和云计费收尾完成。
