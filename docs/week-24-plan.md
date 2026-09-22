# Week 24 Plan: LeaderWorkerSet + Kueue：Gang Admission 与拓扑调度

> 时间预算：11 小时（Day 1–7 合计）
>
> 本周主线：把 Week 23 冻结的双节点 vLLM 副本封装为 LeaderWorkerSet replica group，用 Kueue 验证配额 admission、整组等待和 topology-aware placement，再验证 leader/worker 故障语义。
>
> 前置：[Week 23 plan](week-23-plan.md) 的 runtime contract、A1 配置和功能 smoke；阅读：[Week 24 references](week-24-references.md)。下列文件是待完成产出，不代表仓库已有实现。

## 本周目标

1. 用 LWS 正确表达一个 leader 加若干 workers 的复制单元与稳定 identity。
2. 分开验证 LWS 生命周期、Kueue admission/TAS、Kubernetes Pod placement 与 vLLM runtime。
3. 在资源充足和不足场景验证 group 不会以昂贵的半成品长期占用 GPU。
4. 验证 required/preferred topology、leader endpoint 与 group restart 行为。
5. 形成最终跨节点运维交接包，覆盖版本、部署、观测、故障恢复、回滚与资源清理。

## 本周边界

- `spec.replicas` 表示 replica groups 数；`spec.leaderWorkerTemplate.size` 是每组总 Pod 数且包含 leader，因此常规 worker 数为 `size - 1`，不能把 replicas 当作 worker 数。
- LWS 管理 Pod group 的 identity、rollout 与故障生命周期；vLLM native multiprocessing runtime 仍负责组内进程/rank，不因使用 LWS 而消失。
- Kueue 负责 queue、quota、workload admission 和 topology-aware scheduling 决策；Kubernetes scheduler 最终把 Pods 绑定到 nodes。
- 主实验使用 Kueue 的 admission/TAS。LWS 自身 gang 能力若在固定版本仍为 alpha，只做标记或按需 smoke，不作为稳定生产结论。
- 功能环境仍可使用 `2 nodes × 1 L4` 和普通 TCP；本周比较 controller/scheduling 语义，不重跑或扩展跨节点性能结论。
- 固定 Kubernetes、LWS、Kueue、vLLM、image/model revision；安装前保存 CRD/API compatibility，不直接部署漂移的 `latest` manifest。
- 本周只使用 Week 23 冻结的 native multiprocessing runtime，不增加第二套 runtime/control plane，也不开启 autoscaling；一个 Service 只暴露 leader/API Pod，worker 不作为独立模型副本接流量。

## 本周最终产出

- `docs/lws-kueue-contract.md`：对象 ownership、group identity、admission、topology 与失败状态机。
- `configs/week24-lws/`：固定版本 LWS、Queue/ClusterQueue、ResourceFlavor/Topology 和 vLLM manifests。
- `scripts/run_week24_lws.sh`：部署、事件采集、资源不足/TAS/故障注入与清理。
- `results/week24/`：conditions、events、Pod/node mapping、阶段时间线、请求和恢复数据。
- `reports/week24.md`：matrix 结果、未满足条件、控制器边界和最终跨节点运维 handoff。
- `runbooks/cross-node-operations.md`：版本矩阵、部署/回滚、状态判读、故障处置、清理与 owner handoff。

## 四层 Contract

| 层次 | 本周职责 | 必须观察的状态 | 不负责什么 |
|---|---|---|---|
| LWS | 创建 leader/worker group，维护 identity、rollout 与配置的 restart policy | group/replica index、Pod owner、revision、restart | 不执行 TP/PP collective，不替代 Kueue quota |
| Kueue | queue/quota admission，按配置选择 topology domain | admitted/queued condition、reservation、原因、等待时长 | 不启动 vLLM rank，不等于 Pod 已经 Running |
| Kubernetes scheduler | 根据已准入 PodSpec 将 Pods 绑定 nodes | Pending reason、node、zone/rack label、调度事件 | 不理解 vLLM actor/rank 或模型 readiness |
| vLLM native multiprocessing runtime | rendezvous、rank join、模型加载和 API serving | world size、rank mapping、worker join、service ready | 不决定集群配额或创建 LWS group |

只有 Kueue admitted、全部 Pods scheduled/Ready、全部 ranks joined 且 API smoke 通过后，replica 才标记为 serving。每层阶段时间必须分别记录，不能用一个总 startup 时间隐藏卡点。

## 实验矩阵

| Cell | 场景 | 预期与验证点 |
|---|---|---|
| B0 | 1 group：1 leader + 1 worker；资源充足 | 复现 Week 23 A1，唯一 leader endpoint，rank/Pod identity 对齐 |
| B1 | LWS + Kueue；quota/topology 充足 | 整组 admission 后全部 Pods 调度并服务，保存 admission 到 readiness 时间线 |
| B2 | 配额或 GPU 不足 | Workload 等待且不留下长期占 GPU 的半组；原因可审计 |
| B3 | TAS `required`；完整 group 可放入目标 domain | 同组 Pods 满足冻结的 topology contract，实际 node labels 与 reservation 一致 |
| B4 | TAS `required`；任一 domain 都放不下完整 group | 保持未准入/等待，不静默跨 domain 降级 |
| B5 | TAS `preferred` 负向对照 | 记录偏好无法满足时的实际 placement；不把它解释成 required 保证 |
| B6 | 删除 worker、leader，按需驱逐 node | restart/recreate 与配置一致，旧 rank/endpoint/资源最终清理 |

若 B2 已被 Kueue 准入但 Pod 仍因 scheduler/镜像/运行时原因 Pending，必须归到 admission 后故障，不能称为 Kueue gang 失败。

## 生命周期与流量验收

- Leader/worker labels、replica index、hostname/rank mapping 和 headless discovery 必须可从 manifest 与运行日志相互验证。
- Readiness 只有在完整 world size 加入且模型可生成后才通过；Service selector 不得把 worker Pod 暴露为独立 API replica。
- 分别记录 queue wait、quota reservation、Pod scheduling、image pull、runtime bootstrap、model load 和 request-ready。
- 删除 worker 与 leader 时，保存 group restart policy、在途 streaming 结果、重建顺序、恢复时间和旧资源释放。
- `required` topology 是硬约束；若完整 group 无法放入目标 domain，实验应等待/失败，而不是临时删除约束完成任务。
- 所有性能数字沿用 Week 23 的硬件标签；本周只检查包装后无明显功能异常，不比较生产吞吐。

## 最终跨节点运维 Handoff

- 冻结 Kubernetes、LWS、Kueue、vLLM、image/model revision、CRD checksum、node labels 和全部 manifests，给出从空 namespace 到请求成功的重放顺序。
- 用同一时间线串联 `queued → admitted → Pods scheduled → ranks joined → model loaded → request ready`，并为每个卡点给出 owner、只读诊断、恢复动作和完成信号。
- 提供 worker/leader 故障、quota/topology 不满足、镜像拉取失败与 runtime join 失败的 runbook；每项操作写明 blast radius、回滚和超时上限。
- 完成一次 clean-environment 或 tabletop 重放，确认 Service 只指向完整 Ready 的 leader，旧 revision、reservation、Pods、端口、GPU 与云资源均可收敛。
- 将硬件与网络限制、deferred 性能实验和不可外推结论写入最终报告；本周结束后不保留未登记的计费资源。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 固定 LWS/Kueue 版本，画出 CR ownership、group identity 与四层状态机 |
| Day 2 | 1.5 h | 部署 B0，验证 leader endpoint、rank mapping、readiness 与 streaming |
| Day 3 | 2 h | 配置 Queue/ClusterQueue，运行 B1/B2 并拆分 admission/readiness 时间 |
| Day 4 | 2 h | 配置 Topology/ResourceFlavor，运行 B3/B4 required 场景 |
| Day 5 | 1.5 h | 运行 B5，对照 preferred、实际 placement 与 topology labels |
| Day 6 | 1.5 h | 运行 B6，验证 leader/worker/node failure 与整组资源清理 |
| Day 7 | 1 h | 完成最终跨节点运维 handoff、tabletop 重放、证据归档与计费资源收尾 |

## 报告必须回答的问题

1. 一个 LWS replica group 包含哪些 Pods，`replicas`、group size 与 rank 如何对应？
2. LWS、Kueue、Kubernetes scheduler 与 vLLM runtime 分别拥有哪一段状态？
3. 资源不足时卡在 admission、Pod scheduling、runtime join 还是 readiness，证据是什么？
4. `required` 与 `preferred` topology 的实际行为有何差异？
5. Service 为什么只指向 leader，worker 暴露会破坏什么副本语义？
6. Leader/worker 丢失后的 restart scope、请求影响和资源回收是否符合配置？
7. 最终运维 handoff 是否足以让接手者重放部署、定位每层卡点、回滚并确认所有资源收敛？

## 完成标准

- [ ] B0–B6 均有 manifest、conditions/events、Pod/node mapping 和明确通过/阻塞状态。
- [ ] Contract 清楚区分 LWS lifecycle、Kueue admission/TAS、Pod placement 与 vLLM runtime。
- [ ] 资源不足场景未形成长期占 GPU 的半组；若发生，已定位具体层和释放方法。
- [ ] TAS required 可行/不可行场景均已验证，不把 preferred placement 当硬保证。
- [ ] Service 只选择完整 Ready 的 leader/API endpoint，worker 不作为独立 replica 接流量。
- [ ] Leader 与 worker 故障至少各注入一次，旧 rank、Pod、端口和 GPU 最终清理。
- [ ] 未把 LWS/Kueue 功能 smoke 或普通 TCP 数据写成生产性能结论。
- [ ] 最终跨节点运维 runbook 已完成一次 clean-environment 或 tabletop 重放，owner、诊断、恢复、回滚和完成信号均明确。
- [ ] 结果同步并绑定 Git commit/image/CRD versions，queue reservation 与云资源全部收尾。
