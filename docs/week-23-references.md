# Week 23 Reference Reading

第二十三周只补 vLLM 多节点中的 DP、EP 和通信测量资料。TP/PP 与 distributed serving 的基础继续复用 Week 14，不重复收录相同 URL。

以下官方页面已于 2026-09-22 核对。vLLM 的 `stable` 与 `latest` 页面可能对应不同发布线；实验必须保存实际 revision、image digest 和本地 `vllm serve --help`。

## 新增必读：DP 与通信

1. [vLLM Data Parallel Deployment](https://docs.vllm.ai/en/stable/serving/data_parallel_deployment/)
   - 阅读 DP deployment、hybrid load balancing 和各 DP rank 的服务关系。
   - 区分一个模型副本内的 TP/PP 与多个完整 engine replicas；只采用固定版本确实支持的启动方式。
   - 本周只验证最小 DP 请求分发，不引入生产 load balancer 或 autoscaling。

2. [NVIDIA Multi-Node Tuning Guide: Measuring Performance](https://docs.nvidia.com/multi-node-nvlink-systems/multi-node-tuning-guide/measuring-performance.html)
   - 阅读 NCCL tests、algorithm/bus bandwidth、message sizes 和性能测量注意事项。
   - 先确认 collective transport 和拓扑，再解释 vLLM 差异；不把普通 TCP smoke 当作 GPU-direct 结果。
   - 该指南面向特定高性能系统，不能把其中硬件带宽直接套到 L4 环境。

3. [vLLM Distributed Troubleshooting](https://docs.vllm.ai/en/stable/serving/distributed_troubleshooting/)
   - 阅读 network interface、NCCL transport、shared memory、container IPC 与多节点连通性排障。
   - 先运行最小 collective/worker smoke，再运行 serving；环境错误不能靠调 TP/PP 参数掩盖。
   - 多节点控制与数据通道只放在受信任私网，固定版本的安全说明优先于示例命令。

## 按需新增：Expert Parallel

4. [vLLM Expert Parallel Deployment](https://docs.vllm.ai/en/latest/serving/expert_parallel_deployment/)
   - 只在有受支持 MoE 模型和足够 GPU 时阅读 deployment、expert placement 与 communication backend。
   - 区分 DP、EP 与 TP，记录 all-to-all 和 load-balance 前提。
   - 没有合适硬件时完成设计并标记 deferred，不用 dense model 或两卡 smoke 推断生产 EP 收益。

## 复用：只查本周增量问题

| 已有来源 | 本周只查什么 |
|---|---|
| [Week 14 references](week-14-references.md) #1 | TP/PP 部署建议、单/多节点策略和通信排障；这是 vLLM Parallelism and Scaling 的唯一收录位置 |
| [Week 4 references](week-04-references.md) #4–5、#7–9 | 固定版本 CLI、benchmark、metrics 和 engine args，不重复收录 `serve` URL |
| [Week 12 references](week-12-references.md) #1–4 | 短通信 timeline 与 NVTX/Nsight 证据，不把 profiler run 当正式性能结果 |
| [Week 22 references](week-22-references.md) #1–2 | 复用 SLO 分母、重复单位与不确定性规则 |

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 复用 Week 14 #1；新增 1 | TP/PP/DP/EP 与 runtime topology |
| Day 2 | 新增 2–3；复用 Week 4 #4/#8 | 通信、CLI 和环境 preflight |
| Day 3 | 复用 Week 14 #1 | A0/A1 跨节点 PP smoke |
| Day 4 | 新增 2–3 | A2 collective 诊断与结论边界 |
| Day 5 | 新增 1 | A3 DP 请求分发 |
| Day 6 | 新增 3；按需新增 4 | 故障注入；硬件满足时做 EP/A4 |
| Day 7 | 回看 contract 与原始结果 | Runtime decision table |

## 阅读后的自测问题

1. TP、PP、DP、EP 分别复制或切分哪些状态？
2. 为什么 `TP = 每节点 GPU 数、PP = 节点数` 是常见建议，却不是普适最优值？
3. Ray/native multiprocessing 与 NCCL collective 分别负责哪一层？
4. 为什么 DP=2 的吞吐结果不能回答一个大模型如何跨两节点放下？
5. NCCL algorithm bandwidth 与 bus bandwidth 有什么差别，message size 为什么重要？
6. 两台 L4 通过普通 TCP 成功生成，为何仍不能说明跨节点 TP 具有生产性能？
7. EP 实验为什么必须同时说明模型类型、expert placement、负载均衡与网络？
