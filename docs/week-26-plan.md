# Week 26 Plan: 同硬件 LWS vs KubeRay ADR 与多节点验证

> 时间预算：约 11 小时，其中 AIBrix `RayClusterFleet` mapping 最多 1–1.5 小时且为可选
>
> 本周主线：在相同硬件、vLLM/model/workload 和 Kueue contract 下，对 LWS 路径与 KubeRay/RayService 路径做整套栈的生命周期与运维 ADR；稳态请求数据只用于回归检查，不用普通 TCP 小样本宣布平台性能胜负。
>
> 前置：[Week 24 plan](week-24-plan.md) 的 LWS/Kueue 证据和 [Week 25 plan](week-25-plan.md) 的 KubeRay/RayService/PG 证据；阅读：[Week 26 references](week-26-references.md)。下列文件是待完成产出，不代表仓库已有实现。

## 本周目标

1. 冻结同硬件公平比较 contract，并明确两条路径比较的是完整部署栈而非同一 runtime 的可替换 controller。
2. 分阶段比较 admission、Pod scheduling、runtime bootstrap、model load、readiness、恢复和升级。
3. 在相同故障时点验证 worker/group/head/actor 的 blast radius 与资源收敛。
4. 用可审计 decision matrix 给出条件化选择，而不是只按 throughput 选赢家。
5. 最多用 1–1.5 小时把 KubeRay 设计映射到 AIBrix `RayClusterFleet`；它不进入主 campaign。

## 本周边界

- LWS 与 KubeRay 不是 runtime 替代关系：LWS 管 leader/worker Pod group，应用仍需 distributed runtime；KubeRay 管 Ray cluster，RayService 进一步管理 Ray Serve application。
- 两者只在“谁拥有一个跨节点推理副本的 Kubernetes Pods/lifecycle”这一窄决策上重叠；runtime API、serving control plane、failure 和 upgrade 语义不同。
- 主对照是两条明确栈：`LWS → 固定 vLLM distributed runtime` 与 `KubeRay RayService → Ray Serve → vLLM + placement group`。因此结果用于 ADR 总体取舍，不能声称某个 operator 单独造成性能差异。
- 固定 node pool、GPU SKU/count、nodes per replica、TP/PP、model revision、dtype/quantization、engine args、image layers（可共享部分）、storage/cache、network、ingress contract、workload、warmup、replica count 和运行顺序。
- 固定相同 Kueue ClusterQueue、quota、priority、Topology/ResourceFlavor 与 admission 条件；所有 autoscaler 关闭，admission wait 与 runtime startup 分开。
- `2 nodes × 1 L4` + 普通 TCP 只用于功能、生命周期和故障 smoke。普通 TCP 也可报告绑定该硬件/transport 的重复测量，但不能宣称生产代表性性能；后者需要匹配目标生产环境的多卡 topology、节点内互联、跨节点 transport 与通过门槛的 NCCL 证据。
- `RayClusterFleet` 只做 manifest/object mapping，不运行第三套完整性能矩阵，也不作为 LWS compatibility layer。

## 本周最终产出

- `docs/adr/0001-multinode-serving-controller.md`：context、options、evidence、decision、consequences 与 revisit triggers。
- `configs/week26-comparison/`：两条栈的冻结 manifests、Kueue contract 和故障/升级矩阵。
- `scripts/run_week26_comparison.sh`：配对顺序、阶段计时、故障注入、rollback 与清理。
- `results/week26/`：对象状态、阶段时间、GPU reservation-seconds、请求回归和运维证据。
- `reports/week26.md`：公平性检查、decision matrix、硬件结论边界和限制。
- `configs/week26-rayclusterfleet-mapping.yaml`：仅在可选支线完成时保存，不作为主结果依赖。

## 同硬件比较 Contract

| 维度 | LWS 路径 | KubeRay 路径 | 公平性与解释 |
|---|---|---|---|
| Workload object | LWS replica group | RayCluster/RayService | 对比 Pod/lifecycle ownership，不称 API 等价 |
| Runtime | Week 23 冻结的 vLLM distributed backend | Ray runtime + Ray Serve + vLLM PG | runtime stack 不同，稳态差异只归到整套栈 |
| Admission | 同一 Kueue queue/quota/topology contract | 同一 Kueue queue/quota/topology contract | 单独报告 queue/admission latency |
| Serving endpoint | 完整 Ready leader/API endpoint | RayService/Serve endpoint | 保持外部 request schema、client 与 ingress hops 可审计 |
| Failure | leader/worker group policy | Ray head/worker/actor reconcile | 使用相同故障时点，比较 blast radius 与恢复语义 |
| Engine image/config update | LWS Pod-group revision/rollout | `rayClusterConfig` cluster upgrade/replacement | D3 使用相同 vLLM image/engine change，记录不可用窗口与 rollback |
| Application-only update | 无独立 Serve control plane；需更新 Pod template/runtime | `serveConfigV2` application update | 复用 Week 25 C6a 作为能力差异，不与 D3 的配对 downtime 合并 |
| Runtime placement | Pod topology + vLLM ranks | Pod topology + Ray PG bundles/actors | Kueue admission 不等于 PG，分别保留状态 |

每个配对 cell 先运行 LWS/KubeRay 的顺序随机或交错，再交换顺序复查。任何无法固定的镜像、network path、runtime feature 或 API hop 都写入 ADR 的 confounders，不用归一化数字掩盖。

## 必跑矩阵与指标

| Cell | 配对场景 | 必须比较的证据 |
|---|---|---|
| D0 | 单 replica 创建到首个成功请求 | admission、schedule/image pull、runtime/model、readiness 各阶段时间 |
| D1 | 相同资源不足 + required topology | queued/admitted、partial allocation、Pending 原因和 GPU reservation-seconds |
| D2 | 相同时间删除一个 GPU worker | 在途/新请求、重建 scope、恢复时间、旧 rank/actor/Pod 清理 |
| D3 | 相同 vLLM image/engine config update | LWS group rollout 对 RayService `rayClusterConfig` cluster upgrade；顺序、不可用窗口、版本重叠与 rollback |
| D4 | 扩为第二个跨节点 replica | quota、Pod/PG allocation、endpoint readiness 与撤销后的资源收敛 |
| D5 | 固定请求 trace 的短稳态回归 | TTFT、TPOT、goodput、错误率、GPU utilization，仅检查异常开销 |

另外统计 manifests/CRDs/controllers 数量、状态面数量、一次问题定位所需跳数、observability/debugging surface、升级 blast radius、idle control-plane resources 与 allocated/billed GPU-hours。不要因 LWS 功能更窄就称其必然更快，也不要因 Ray API 更丰富就称 KubeRay 必然更优。

## 硬件证据门槛

| 结论等级 | 最低环境 | 允许的结论 |
|---|---|---|
| 功能 smoke | 2 个同 zone 节点 × 1 L4；普通 TCP | CR lifecycle、gang/TAS、rank/actor、API、故障与清理可工作 |
| 教学型拓扑对照 | 两个同型号节点、每节点多张同型号 GPU，记录实际节点内/节点间 topology | 同节点与跨节点配置的机制差异；仍需披露网络限制 |
| 硬件范围内的通信性能 | 同型号多卡节点、已知节点内 topology、实测足够的节点间带宽且 NCCL tests 通过 | 明确记录 `TCP`、`GPUDirect-TCPX` 或 `RDMA` 等实际 transport，只对固定环境/workload 报告重复结果 |
| 生产代表性通信性能 | 硬件、节点内 fabric、跨节点 transport、placement 和规模与目标生产环境匹配 | 只代表冻结的目标环境，不外推为所有生产集群 |

达不到对应门槛时降低结论等级或 deferred，不能用普通 TCP 的成功率/吞吐补写生产性能结论。

## 可选：AIBrix `RayClusterFleet` Mapping

本支线最多 1–1.5 小时，只在主 ADR 证据齐全且所选 AIBrix release 的 CRDs 可核对时进行：

1. 把 Week 25 的 RayCluster template 映射到文档中的 `RayClusterReplicaSet`/`RayClusterFleet` 层次。
2. 标出 template、replica rollout、model/gateway labels 和既有 KubeRay ownership 的增量。
3. 若 CRD/schema 与固定 release 不一致，记录差异后停止；不为兼容最新文档升级整套环境。
4. 不运行完整 workload/performance campaign，不把它列为与 LWS/KubeRay 并列的第三种通用 runtime。

只有项目已采用 AIBrix 且确实需要多个 multi-node Ray replicas 的 fleet rollout/autoscaling 时，才在后续把它升级为必修。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 冻结两条栈、公平比较 contract、结论等级和 ADR 选择标准 |
| Day 2 | 2 h | 运行 D0，拆分 admission 到 request-ready 阶段并交换运行顺序 |
| Day 3 | 2 h | 运行 D1/D5，核对 gang/topology 与短稳态功能回归 |
| Day 4 | 1.5 h | 运行 D2，比较 worker failure、blast radius 与资源清理 |
| Day 5 | 1.5 h | 运行 D3/D4，比较 upgrade、第二 replica 和 rollback |
| Day 6 | 1.5 h | 二选一：做不超过 1.5 h 的 RayClusterFleet mapping；或补齐成本/复杂度证据审计，两者不叠加 |
| Day 7 | 1 h | 汇总既有成本/复杂度，完成 ADR、限制与 revisit triggers，清理并同步证据 |

## 报告必须回答的问题

1. 哪些变量完全相同，哪些 runtime/serving 差异无法消除并因此只能做整套栈 ADR？
2. 两条路径从 Kueue admission 到首个成功请求各阶段耗时和失败点是什么？
3. Worker failure、配对 engine/cluster upgrade 和第二 replica 的 blast radius、不可用窗口与清理语义有何不同？Serve application-only update 又增加了什么能力？
4. 哪条路径需要更少的状态面与调试跳数，缺失或新增了哪些真正需要的能力？
5. 功能 smoke、教学型拓扑观察和通信性能结论分别满足了哪个硬件门槛？
6. 最终选择由哪些需求决定，哪些证据会触发重新评估？
7. `RayClusterFleet` mapping 增加了什么，为何没有进入主对照或成为第三种 runtime？

## 完成标准

- [ ] D0–D5 均按同硬件 contract 成对执行，顺序、版本、confounders 和失败均保留。
- [ ] ADR 明确 LWS/KubeRay 不是 runtime 替代关系，结论覆盖 scheduling、runtime、failure、upgrade、observability、复杂度和成本。
- [ ] Kueue admission、Pod scheduling、runtime/PG、model load 与 request-ready 分阶段报告。
- [ ] 至少完成配对 worker failure、update/rollback 和第二 replica 资源收敛。
- [ ] 普通 TCP/L4 的 D5 只作回归检查，没有平台胜负或生产性能措辞。
- [ ] 所有测量绑定实际 topology/transport；普通 TCP 结果只限固定硬件，生产代表性结论另满足目标环境与 NCCL 门槛。
- [ ] `RayClusterFleet` 最多投入 1–1.5 小时；未做也不影响主 ADR 完成，做了也只作为 optional mapping。
- [ ] 结果同步并绑定 Git commit/image/CRD versions，queue、PG、Pods、nodes、storage 和 LB 计费全部收尾。
