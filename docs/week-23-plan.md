# Week 23 Plan: vLLM Native Multiprocessing 多节点 Runtime Contract

> 时间预算：11 小时（Day 1–7 合计）
>
> 本周主线：冻结 TP、PP、DP、EP 与 native multiprocessing runtime 的职责边界，在最小双节点环境验证一个模型副本如何跨节点运行；先证明拓扑、通信和失败语义正确，再决定硬件是否足以支持性能结论。
>
> 前置：[Week 22 plan](week-22-plan.md) 的 serving baseline、固定 workload 与实验口径，以及 [Week 14 plan](week-14-plan.md) 的单节点 TP 结论；阅读：[Week 23 references](week-23-references.md)。下列文件是待完成产出，不代表仓库已有实现。

## 本周目标

1. 画清 TP、PP、DP、EP 的 rank、权重、KV cache 和请求复制关系。
2. 把 vLLM 并行参数、native multiprocessing 启动参数、物理节点/GPU 和 NCCL transport 固化为可审计 contract。
3. 在 `2 nodes × 1 L4` 上完成跨节点 PP、TP 诊断和 DP 的功能 smoke。
4. 区分“模型可以运行”“结果正确”和“通信性能有代表性”三种证据。
5. 为 Week 24 冻结同一镜像、模型、请求、失败注入和测量阶段，并为最终跨节点运维收尾提供 runtime 基线。

## 本周边界

- 功能主线使用两个同 zone 节点、每节点一个 L4 和普通 TCP；它足以验证启动、rank、生成、streaming、故障与清理，不足以宣称生产级跨节点 TP/EP 性能。
- 有意义的通信性能实验必须使用同型号多卡节点、节点内高速互联和节点间高带宽 GPU 网络，并先通过 NCCL collective 测试；没有该硬件就明确标记 deferred。
- 本周不安装 LWS 或 Kueue，不把 Kubernetes controller 行为混入 vLLM runtime 结论。
- 可执行主线只使用冻结版本明确支持的 vLLM native multiprocessing 多节点模式。只有该版本的官方文档与镜像内 CLI help 同时给出 external launcher contract 时，才可把 external launcher 加为独立可选 cell；否则标记为未验证，不推测命令、rank 语义或恢复行为。
- 固定 vLLM revision、image digest、model revision、dtype、quantization、engine args、tokenizer 和 workload；不跨镜像拼接结果。
- 多节点控制与数据通道只放在受信任私网，端口不暴露到公网；凭证、prompt 和模型访问令牌不写入结果。
- EP 只在受支持的 MoE 模型和足够 GPU 上做；没有至少 4 GPU 的合适环境时只完成设计，不用 dense model 模拟 expert balance。

## 本周最终产出

- `docs/multinode-runtime-contract.md`：并行维度、rank mapping、runtime、网络、安全和失败语义。
- `configs/week23-parallelism.yaml`：模型、硬件、TP/PP/DP/EP、backend 与 workload 矩阵。
- `scripts/run_week23_multinode.sh`：preflight、NCCL smoke、vLLM smoke、故障注入与资源清理。
- `results/week23/`：拓扑、日志、collective、逐请求指标、失败事件和成本原始数据。
- `reports/week23.md`：功能证据、硬件限制、可比较 cell 与 Week 24 handoff。

## Parallelism 与 Runtime Contract

| 层次 | 必须记录 | 不应混淆的结论 |
|---|---|---|
| 模型并行 | TP/PP degree、world size、每个 rank 的 node/GPU、权重与 KV 分片 | 模型终于能放下不等于吞吐或成本效率提升 |
| 数据并行 | DP rank、请求如何分发、每个 DP replica 的完整逻辑模型与独立 KV/request state，以及 replica 内 TP/PP 分片关系 | DP=2 是两个 engine replicas，不是一个副本跨两节点 |
| 专家并行 | MoE model、expert placement、DP/EP 组合和 all-to-all transport | 功能 smoke 不代表 expert balance 或生产收益 |
| Runtime launcher | native multiprocessing；仅在冻结版本有官方 contract 时评估 external launcher；启动参数、rendezvous 地址、worker join | Launcher 负责进程启动与 rendezvous，不替代 TP/PP collective，也不自动定义 DP 请求分发 |
| 通信 | NCCL/CUDA/driver 版本、NIC、transport、带宽、延迟、`/dev/shm` 与 IPC | Socket 可用不等于 RDMA/GPU-direct 性能 |
| 服务 | 唯一 API endpoint、readiness、streaming、取消和错误传播 | Worker 进程就绪不等于整个模型副本可接流量 |

每次运行保存实际解析后的 parallel config 和 rank mapping。`TP × PP`、DP replicas 与可见 GPU 数不匹配时直接失败，不通过隐式 oversubscription 完成 smoke。

## Native Multiprocessing 启动 Contract

先固定 vLLM revision 与 image digest，并从同一镜像保存 `vllm serve --help`。以下是本周必须核对的双节点命令形状，不是跨版本稳定 API；参数名、是否可组合及默认值最终以冻结版本的官方文档和镜像内 CLI 为准。任一字段不存在或语义不一致时停止该 cell，不用相似参数猜测替代：

```bash
# Head node
vllm serve "$MODEL" \
  --tensor-parallel-size "$TP" \
  --pipeline-parallel-size "$PP" \
  --nnodes 2 \
  --node-rank 0 \
  --master-addr "$HEAD_NODE_PRIVATE_IP"

# Worker node
vllm serve "$MODEL" \
  --tensor-parallel-size "$TP" \
  --pipeline-parallel-size "$PP" \
  --nnodes 2 \
  --node-rank 1 \
  --master-addr "$HEAD_NODE_PRIVATE_IP" \
  --headless
```

- Head 与 worker 必须使用同一镜像、模型路径、并行参数和可达的私网 `--master-addr`；保存解析后的监听地址与实际端口，不把控制通道暴露到公网。
- `--node-rank` 是节点启动身份，不等同于 TP/PP/DP rank；报告必须从日志重建 node → process → model rank → GPU mapping。
- Worker 的 `--headless` 表示它不提供独立 API endpoint；客户端只访问 head 的服务端点，并在完整 world size 加入、模型加载和生成 smoke 全部成功后才判定 Ready。
- DP 与 EP 只使用冻结版本官方文档和 CLI 明确支持的参数组合；不能把上述 TP/PP 命令机械改写成未经支持的 DP/EP launcher。

## 实验矩阵

| Cell | 资源与配置 | 目的 | 结论边界 |
|---|---|---|---|
| A0 | 1 node × 1 L4；TP=1、PP=1、DP=1 | 正确性、模型输出和测量基线 | 只与同镜像/模型的 cell 比较 |
| A1 | 2 nodes × 1 L4；TP=1、PP=2、DP=1 | 一个模型副本的跨节点 pipeline smoke | 普通 TCP 只证明功能，不宣称 PP 性能 |
| A2 | 2 nodes × 1 L4；TP=2、PP=1、DP=1 | 跨节点 collective 与排障路径 | 作为网络敏感诊断，不作为推荐拓扑 |
| A3 | 2 nodes × 1 L4；TP=1、PP=1、DP=2 | 两个完整 engine replicas 与请求分发 | 与 A1/A2 的模型并行语义分开 |
| A4，可选 | 同型号多卡节点 + 高速节点间网络；固定 GPU 总预算 | TP-within-node、PP-across-node 或 EP 性能验证 | 只有 NCCL 与重复均达门槛才形成性能结论 |

在 A0–A3 先验证固定 prompt 的 token/output contract，再做 streaming、并发和长请求取消。A4 不存在时，保留实验清单和资源门槛，不用 A2 的 TCP 数字填补。

## 测量与失败 Contract

- 运行前检查镜像/模型一致性、GPU visibility、主机名与私网地址解析、端口连通、clock、共享内存和 NCCL transport。
- 分开记录环境准备、native multiprocessing rendezvous、worker join、模型加载、service readiness、warmup 和稳态窗口；启动时间不混入 steady-state TTFT/TPOT。
- 每个可比较性能 cell 至少三个独立重复，并与等 GPU 预算 baseline 交错；保存 TTFT、TPOT、goodput、错误率、GPU memory/utilization 和 GPU-seconds/request。
- NCCL 数字必须同时保存 collective、message size、rank 数、algorithm bandwidth、bus bandwidth 和 transport；单个小消息结果不能代表 serving workload。
- 杀死一个非 head worker，记录新请求、在途 streaming、runtime 状态、退出码和 GPU/端口释放；不把自动重试隐藏在成功率里。
- 普通 TCP/L4 的延迟与吞吐只进入“功能环境观察”；报告不得用“scale-up”“生产可用”或跨硬件 speedup 描述它。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 画出 TP/PP/DP/EP 拓扑，冻结 native multiprocessing、rank 与资源 contract |
| Day 2 | 1.5 h | 固定镜像/模型/网络，完成私网、GPU、NCCL 和清理 preflight |
| Day 3 | 2 h | 运行 A0/A1，验证跨节点 PP 启动、输出、streaming 与阶段计时 |
| Day 4 | 2 h | 运行 A2，定位 collective transport；明确普通 TCP 结论边界 |
| Day 5 | 1.5 h | 运行 A3，验证 DP 请求分发与模型并行的状态差异 |
| Day 6 | 1.5 h | Worker 故障与资源释放；有合适硬件才运行 A4/EP |
| Day 7 | 1 h | 整理 decision table、限制、成本和 Week 24 runtime handoff |

## 报告必须回答的问题

1. 每个 cell 的 TP、PP、DP、EP、world size、rank 与物理 GPU 如何对应？
2. Head/worker 的 `--nnodes`、`--node-rank`、`--master-addr` 与 `--headless` 如何解析，native multiprocessing 负责什么，又不负责什么？
3. A1/A2/A3 分别证明了模型并行、collective 和数据并行的什么差异？
4. NCCL 实际使用什么 transport，哪些网络证据阻止当前结果成为生产性能结论？
5. “模型可以运行”“输出正确”“性能有代表性”的验收证据分别是什么？
6. Worker 丢失时，在途请求、进程、GPU 与 endpoint 如何收敛？
7. Week 24 必须保持哪些 runtime 参数不变，才能只研究 LWS/Kueue？

## 完成标准

- [ ] A0–A3 的 manifest/config、rank mapping、日志和输出可重放；不能运行的 cell 有明确 blocker。
- [ ] TP/PP、DP/EP 和 native multiprocessing 的职责边界写入 contract，没有把 launcher 或 Kubernetes 当作 collective。
- [ ] Head/worker 命令、镜像内 `vllm serve --help`、解析后配置和 node/process/model-rank/GPU mapping 已一起保存。
- [ ] `2 nodes × 1 L4` 的结果只标为功能 smoke/诊断，没有生产级性能措辞。
- [ ] NCCL transport、拓扑与硬件证据齐全；A4 缺少合适硬件时明确 deferred。
- [ ] Streaming、取消和单 worker 故障至少各验证一次，残留进程/端口/GPU 已检查。
- [ ] 性能数据只比较同模型、同镜像、同 GPU 预算和同 workload 的独立重复。
- [ ] 多节点端口留在受信任私网，日志与结果不含 prompt、凭证或模型令牌。
- [ ] 结果同步并绑定 Git commit/image digest，节点、磁盘和负载均衡计费收尾完成。
