# Week 26 Reference Reading

第二十六周不重复列出 LWS、Kueue、KubeRay、RayService 或 placement group 文档；主阅读是前两周的运行证据。本周只新增可选 AIBrix fleet mapping 与硬件/网络结论边界。

以下官方页面已于 2026-09-22 核对。云 GPU SKU、网络能力、区域和 GKE 功能会变化；实验必须保存实际 machine type、zone、NIC/driver、placement、quota 和 NCCL 输出，不能仅引用产品页。

## 可选新增：AIBrix Multi-Node Mapping

1. [AIBrix Multi-Node Inference](https://aibrix.readthedocs.io/latest/features/multi-node-inference.html)
   - 只读 `RayClusterReplicaSet`/`RayClusterFleet` 的对象层次、template、replicas 与 rollout。
   - 将 Week 25 已验证的 KubeRay design 做 1–1.5 小时 manifest mapping；不重跑完整性能矩阵。
   - 它是 AIBrix-specific fleet abstraction，不是 LWS compatibility layer，也不是第三种 distributed runtime。

## 新增必读：硬件与网络边界

2. [Google Cloud GPU Machine Types](https://cloud.google.com/compute/docs/gpus)
   - 核对候选 GPU machine family、GPU 数量、CPU/memory 和实际可用区域。
   - Week 1 #10 的 G2 创建页面继续用于操作；本页只用于 Week 26 的硬件能力与公平性清单。

3. [Google Cloud GPU Network Bandwidth](https://cloud.google.com/compute/docs/gpus/gpu-network-bandwidth)
   - 核对 machine type、NIC、网络带宽与 GPU workload 的限制，保存实际配置而非产品上限。
   - 普通 TCP、GPUDirect-TCPX 与 RDMA 环境必须分别记录并使用不同结论标签。

4. [GKE GPUDirect-TCPX and Compact Placement](https://cloud.google.com/kubernetes-engine/docs/how-to/gpu-bandwidth-gpudirect-tcpx)
   - 阅读支持条件、cluster/node-pool configuration、compact placement 和验证步骤。
   - GPUDirect-TCPX 不是 RDMA；报告必须写实际 transport，不能用“GPU direct”把不同网络机制合并。
   - 只有环境实际满足支持矩阵且 NCCL 验证通过，才能进入有意义的跨节点通信性能层。
   - 本周不要求为完成 ADR 临时采购生产规模硬件；资源不足时保留设计并降低结论等级。

## 复用：ADR 的主要证据来源

| 已有来源 | 本周只查什么 |
|---|---|
| [Week 23 references](week-23-references.md) #1–4；[Week 14 references](week-14-references.md) #1 | 固定 parallelism/runtime、NCCL 口径与普通 TCP 结论边界 |
| [Week 24 references](week-24-references.md) #1–7 | LWS group/failure 与 Kueue admission/TAS 的实际 contract |
| [Week 25 references](week-25-references.md) #1–6 | KubeRay/RayService lifecycle、Serve LLM、PG 与 Kueue 两层状态 |
| [Week 22 references](week-22-references.md) #1–2 | SLO 分母、配对重复、失败与不确定性表达 |
| [Week 1 references](week-01-references.md) #10–13 | GCP GPU VM 操作、Spot/driver/zone；不重复收录创建页面 |

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 复用 Week 23–25 contracts；新增 2–4 的适用条件 | 冻结公平性与结论等级 |
| Day 2 | Week 24/25 lifecycle 证据 | D0 启动阶段对照 |
| Day 3 | Week 24 #5–7、Week 25 #5–6；新增 3–4 | Gang/topology 与稳态边界 |
| Day 4 | Week 24 #4、Week 25 #2/#5 | 故障和资源清理 |
| Day 5 | Week 24 #1–2、Week 25 #1–2 | Upgrade、第二 replica 与 rollback |
| Day 6 | 新增 2–4；按需新增 1 | 成本/硬件限制与 optional mapping |
| Day 7 | ADR 与全部原始证据 | Decision、consequences 与 revisit triggers |

## 阅读后的自测问题

1. 为什么 LWS 与 KubeRay 只能比较完整部署栈，不能写成彼此的 distributed runtime 替代品？
2. 哪些变量能公平固定，哪些 API/runtime 差异必须作为 ADR 的组成部分保留？
3. Admission wait、Pod scheduling、runtime bootstrap、model load 与 readiness 为什么不能合成一个数字？
4. 两节点普通 TCP L4 环境能支持哪些结论，明确不能支持哪些结论？
5. 什么硬件、网络、placement 与 NCCL 证据才允许进入通信性能层？
6. LWS 更窄或 Ray API 更丰富，为什么都不能单独决定 ADR 赢家？
7. `RayClusterFleet` 解决哪一层问题，什么时候才值得从 optional mapping 升为必修？
