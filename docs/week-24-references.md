# Week 24 Reference Reading

第二十四周只阅读 LeaderWorkerSet 的 group/lifecycle 原语，以及 Kueue 的 admission 与 topology-aware scheduling。vLLM 多节点 runtime 继续复用 Week 23，不把 controller 文档当作 collective 实现。

以下官方页面已于 2026-09-22 核对。LWS/Kueue 仍在快速演进，示例字段与 feature maturity 必须和固定 release、CRD schema 及 Kubernetes 版本核对。

## 新增必读：LeaderWorkerSet

1. [LeaderWorkerSet Overview](https://lws.sigs.k8s.io/docs/overview/)
   - 先理解一个 replica group 的 leader、workers、稳定 identity 和面向 AI/ML 多 Pod workload 的定位。
   - 核对当前 API maturity；不要把 overview 中的可选能力全部视为稳定默认值。

2. [LeaderWorkerSet Concepts](https://lws.sigs.k8s.io/docs/concepts/leaderworkerset/)
   - 阅读 `replicas`、group size、leader/worker templates、rollout 与 networking 概念。
   - 一个 group 是一个复制/生命周期单元，不把每个 worker 计作可独立接流量的模型副本。

3. [LeaderWorkerSet Basic Example](https://lws.sigs.k8s.io/docs/examples/leaderworkerset/basic/)
   - 对照 leader API、worker 启动、rank/discovery、Service 和 GPU requests。
   - 只移植与固定 vLLM runtime contract 一致的字段，不把示例镜像/tag 当作版本锁。

4. [LeaderWorkerSet Failure Handling](https://lws.sigs.k8s.io/docs/concepts/leaderworkerset/failure-handling/)
   - 阅读 leader/worker restart、group recreation 和 policy 的触发条件。
   - 将文档语义转成删除 leader/worker 的可观察实验，不只检查最终 Pod 数恢复。

## 新增必读：Kueue 与 Topology

5. [Run LeaderWorkerSet with Kueue](https://kueue.sigs.k8s.io/docs/tasks/run/leaderworkerset/)
   - 阅读 queue label、workload admission，以及 leader/worker templates 如何形成 Kueue PodSets。
   - 需要同组 co-location 时，在两个 templates 上声明相同的 required-topology 与 PodSet group identity；执行字段以固定 CRD schema 为准。
   - Kueue admission/reservation 不等于 Pods 已经被 Kubernetes scheduler 绑定到 nodes。

6. [LeaderWorkerSet Topology-Aware Scheduling Example](https://lws.sigs.k8s.io/docs/examples/leaderworkerset/topology-aware-scheduling/)
   - 阅读 LWS、queue、ResourceFlavor/Topology 与 PodSet topology request 如何连接。
   - 保存实际 labels、admission 和 node mapping；示例成功不证明当前集群具备相同 topology。

7. [Kueue Topology-Aware Scheduling](https://kueue.sigs.k8s.io/docs/concepts/topology_aware_scheduling/)
   - 区分 required/preferred request、topology domain、quota reservation 与最终 Pod scheduling。
   - TAS 解决 Kubernetes workload admission/placement，不负责 vLLM rank 或 collective。

## 按需新增：安装核对

8. [LeaderWorkerSet Installation](https://lws.sigs.k8s.io/docs/installation/)
   - 只用于选择兼容 release、安装方式和验证 controller/CRD。
   - 保存实际 chart/manifest version；不从文档复制随时间漂移的 `latest` 作为实验依赖。

## 复用：只查本周增量问题

| 已有来源 | 本周只查什么 |
|---|---|
| [Week 23 references](week-23-references.md)；[Week 14 references](week-14-references.md) #1 | 固定 vLLM runtime、TP/PP 与 rank mapping，不重新学习 parallelism |
| [Week 15 references](week-15-references.md) #1 | Pod readiness、termination 和 endpoint 移除；不把 Running 当作 serving |
| [Week 22 references](week-22-references.md) #1 | 复用 request SLO/失败分母，不用成功请求掩盖 group failure |

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 1–2；按需 8 | CRD、group identity 与版本冻结 |
| Day 2 | 3；复用 Week 23 | 最小 LWS vLLM group |
| Day 3 | 5、7 的 admission 部分 | Queue/quota 与资源不足 |
| Day 4 | 5–7 的 topology 部分 | TAS required 可行/不可行 |
| Day 5 | 7 的 preferred 语义 | 负向对照与实际 placement |
| Day 6 | 4 | Leader/worker failure handling |
| Day 7 | 回看 1–7 和 events | 四层状态机与 Week 25 handoff |

## 阅读后的自测问题

1. `spec.replicas` 与一个 replica group 内的总 Pod 数有什么差别？
2. LWS 管理了哪些生命周期，又为什么没有替代 vLLM distributed executor？
3. Kueue admission、Kubernetes Pod scheduling 和 application readiness 为什么是三个状态？
4. 配额总量足够时，为什么 required topology 仍可能无法准入完整 group？
5. `required` 与 `preferred` topology 失败时应分别出现什么行为？
6. LWS group restart 如何影响在途请求、旧 rank 和 Service endpoint？
7. 为什么 `2 nodes × 1 L4` 能验证 gang/TAS，却不能证明生产多节点推理性能？
