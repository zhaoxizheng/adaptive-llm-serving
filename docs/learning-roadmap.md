# Learning Roadmap: 从 vLLM 到 Cloud-Native LLM Serving

> 目标：用 24 周、通常每周约 10–12 小时，从理解单机 LLM 推理逐步过渡到云厂商通用的 Kubernetes 推理服务架构，并完成一个以 vLLM、Gateway API、Gateway API Inference Extension（GAIE）和 llm-d 为可移植主线的可复现项目。Prometheus 与 Kubernetes 作为已掌握的基础设施直接使用，不再安排基础学习。

## 路线总览

```text
理解单次生成
    ↓
掌握 vLLM 单实例性能
    ↓
读懂 scheduler / KV cache / worker
    ↓
部署多副本 vLLM
    ↓
建立 Gateway API L7 基线
    ↓
加入 InferencePool 与 llm-d EPP
    ↓
验证扩缩容、云可移植性与故障语义
    ↓
完成 vLLM 原生多进程多节点运行与 LWS + Kueue 运维交接
```

最终项目：

> 构建一个基于 vLLM + Kubernetes Gateway API + GAIE `InferencePool` + llm-d EPP 的自适应 LLM Serving 平台，在突发流量、长短请求混合和共享前缀三类负载下，对路由、扩缩容、故障恢复与云实现边界进行可复现实验。HPA/KEDA 是主扩缩容路径；KServe `LLMInferenceService` 作为可选声明式控制面对照，不是可移植数据面的前提。

默认前提：有普通后端开发经验，了解 Python 和 Linux。开发设备是 36 GB 内存的 Mac M3 Pro，日常内存占用可能达到约 30 GB，因此从第一阶段开始就使用按小时计费的云端 NVIDIA GPU；本地 Mac 只负责写代码、Git、查看实验结果、分析数据和撰写文档，不在本地加载模型或运行正式 benchmark。

## 开发与 GPU 环境策略

### 为什么从一开始就使用云端 GPU

- vLLM 的主要学习和性能路径围绕 Linux、NVIDIA CUDA 展开，Apple Silicon/MPS 不适合作为这条路线的基准环境。
- 本地统一内存已经长期处于高占用状态，继续加载模型容易引发 swap、系统卡顿和不可重复的性能结果。
- 从第一天就在 CUDA 环境运行，可以避免前期代码在 MPS/CPU 上可用、迁移到 vLLM 和 CUDA 时又重新适配。
- 后续的 Triton、Nsight、多副本路由和多节点并行实验本来就需要 NVIDIA GPU 或 Linux 集群。

### 环境分工

| 环境 | 负责内容 | 不负责内容 |
|---|---|---|
| Mac M3 Pro | 编辑代码、Git、SSH、阅读源码、画图、分析下载后的指标、写报告 | 加载模型、运行 vLLM、正式性能测试 |
| 单卡云 GPU | Mini Inference Lab、vLLM 单实例、profiling、参数调优 | 多副本和多卡结论 |
| 多卡云主机或 GPU Kubernetes | tensor parallel、多副本路由、Gateway/GAIE、扩缩容、多节点与故障实验 | 日常编码和长期空闲开发 |

### 分阶段 GPU 建议

- 第 1–4 阶段：默认使用 GCP Compute Engine `g2-standard-4` Spot（1×NVIDIA L4 24 GB、4 vCPU、16 GiB 内存），优先从 `us-central1-a` 尝试。它足够运行小模型、vLLM 和大多数单卡实验。若该区没有 Spot 容量，可换同区域的 G2 可用区，或等待后重试。
- tensor parallel 实验：短租一台至少双卡且卡间通信拓扑明确的机器。所有对比应固定 GPU 型号和数量。
- Kubernetes serving 阶段：迁移到 GKE，使用至少两个可调度 GPU 实例或一台多 GPU 节点，确保能真实比较多副本路由。纯 CPU 集群只用于验证 CRD、控制器和 reconciliation，不用于性能结论。
- 多节点阶段：至少准备两个同区、同 GPU 型号的节点。普通 L4/TCP 环境可验证 TP/PP/DP correctness、operator lifecycle 与 failure semantics，但不能代表高带宽生产集群的 collective 性能。
- 跨节点 TP/EP 性能实验只有在模型、GPU 数量、节点内/节点间互联和拓扑证据满足前提时才执行；条件不足时标记 `blocked` 或 `deferred`，不使用 CPU 或不同 GPU 拼成结论。
- 不必一开始租 H100。先用 24 GB GPU 跑通完整方法；只有模型规模或特定 FP8/Hopper 实验确实需要时，再短时使用高端 GPU。

### 当前 GCP 基线

| 项目 | 默认值 |
|---|---|
| 平台 | GCP Compute Engine |
| 机型 | `g2-standard-4` |
| GPU | 1×NVIDIA L4 24 GB |
| 供应方式 | Spot |
| 抢占动作 | `STOP` |
| 启动盘 | 100 GB `pd-balanced` |
| 操作系统 | Ubuntu 24.04 LTS |
| 主区域 | `us-central1` |

Spot VM 可能随时被抢占，因此实验必须按 case 增量写盘并支持断点续跑。停止 VM 后计算资源不再计费，但 Persistent Disk 仍会计费；项目结束且结果同步完毕后应删除 VM 及启动盘。价格、容量和 quota 会变化，每次创建前以 GCP 控制台显示为准。

### 云端工作流

每次租用 GPU 前：

- [ ] 在本地完成代码、配置和测试数据准备
- [ ] 将实验参数写入版本化配置，不在命令行临时拼接
- [ ] 明确本次实验矩阵和停止条件
- [ ] 准备一条环境检查命令和一条完整 benchmark 命令

实例启动后：

- [ ] 记录 GPU 型号、驱动、CUDA、PyTorch、vLLM 和模型版本
- [ ] 使用容器或锁定依赖，避免实例间环境漂移
- [ ] 模型权重和容器层放在可复用缓存或持久卷
- [ ] 先跑 smoke test，再运行完整实验矩阵
- [ ] 将原始结果、日志和环境清单同步回仓库或对象存储

实验结束后：

- [ ] 检查结果文件已经同步
- [ ] 停止或销毁 GPU 实例，不让实例空转
- [ ] 单独检查云硬盘、公网 IP、负载均衡器和 Kubernetes 节点池是否仍在计费
- [ ] 将失败实验也记入实验日志

建议为每次运行生成以下元数据：

```yaml
run_id: 2026-xx-xx-prefix-burst-001
git_commit: <commit>
gpu: NVIDIA-L4-24GB
gpu_count: 1
driver: <version>
cuda: <version>
pytorch: <version>
vllm: <version>
model: <model-and-revision>
workload: prefix-burst
policy: round-robin
started_at: <timestamp>
duration_minutes: <minutes>
estimated_cost: <amount-and-currency>
```

### 成本控制原则

1. 本地准备，云端只执行；不要在计费 GPU 上长时间读文档和写代码。
2. 将下载模型、构建镜像和运行 benchmark 分开，避免反复等待。
3. 先用小 workload 验证，再扩大实验规模。
4. 所有实验脚本支持失败退出，并尽可能设置最大运行时间。
5. 用 Git commit 和运行清单绑定结果，避免因为不可复现而重新租卡。
6. GCP Spot 适合单卡 benchmark，但脚本必须按 case 原子落盘并可断点续跑；需要稳定多节点通信的实验使用按需实例。

## 第一阶段：建立推理性能直觉（第 1–3 周）

### 学习目标

- [ ] 理解 Transformer decoder 的基本数据流
- [ ] 理解 prefill 和 decode 的区别
- [ ] 掌握 KV Cache 的作用及大小估算方法
- [ ] 理解 batch size、sequence length 对显存和延迟的影响
- [ ] 理解 compute-bound 和 memory-bound
- [ ] 掌握 TTFT、TPOT、ITL、E2E latency、throughput 和 goodput
- [ ] 区分 tensor parallel、pipeline parallel 和 data parallel

### 动手项目：Mini Inference Lab

用 Hugging Face 模型完成一个约 300–500 行的实验项目：

- [ ] 实现普通文本生成
- [ ] 分别测量 prefill 和 decode 耗时
- [ ] 比较启用和禁用 KV Cache
- [ ] 实现简单 dynamic batching
- [ ] 输出 TTFT、TPOT、tokens/s 和显存峰值
- [ ] 测试不同 prompt 长度和 output 长度

建议模型：

- 8–12 GB GPU：Qwen2.5-0.5B 或 Qwen2.5-1.5B
- 24 GB GPU：Qwen2.5-3B 或 Qwen2.5-7B
- 当前路线默认选择云端 24 GB NVIDIA GPU；先用 0.5B/1.5B 模型快速调试，再用 3B/7B 模型完成正式实验

### 阶段验收

- [ ] 能解释为什么 prefill 通常更偏计算密集，decode 更偏访存密集
- [ ] 能估算一个模型单请求的 KV Cache 大小
- [ ] 能画出 concurrency 与 TTFT、吞吐之间的关系
- [ ] 能解释 batching 为什么提高吞吐，但可能损害尾延迟

## 第二阶段：把 vLLM 当作用户使用（第 4–6 周）

这一阶段先把 vLLM 当成生产服务使用，不急于阅读内部源码。

### 基础任务

- [ ] 使用 `vllm serve` 启动 OpenAI-compatible API
- [ ] 实现 streaming 和 non-streaming 客户端
- [ ] 使用官方 benchmark 工具压测
- [ ] 将 vLLM 指标接入已有 Prometheus/Grafana，并与 benchmark run 对齐
- [ ] 对比 Hugging Face Transformers 与 vLLM

### 参数实验

- [ ] `max-model-len`
- [ ] `gpu-memory-utilization`
- [ ] `max-num-seqs`
- [ ] `max-num-batched-tokens`
- [ ] prefix caching
- [ ] quantization

### Workload 设计

| Workload | 输入 | 输出 | 模拟场景 |
|---|---:|---:|---|
| Short chat | 短 | 短 | 普通问答 |
| Long context | 长 | 短 | 文档问答 |
| Generation | 短 | 长 | 内容生成 |
| Mixed | 混合 | 混合 | 线上真实流量 |

### Benchmark 报告

- [ ] concurrency–throughput 曲线
- [ ] concurrency–P99 TTFT 曲线
- [ ] KV Cache 使用率
- [ ] GPU 利用率和显存使用
- [ ] 不同参数的性能变化
- [ ] OOM、排队和过载发生的边界
- [ ] 固定硬件、模型、版本、参数和 workload，确保结果可复现

### 阶段验收

给定“P99 TTFT 小于 2 秒”等明确 SLO，能够通过实验选择合理的并发、batch 和显存参数，而不只是笼统地说 vLLM 更快。

## 第三阶段：读懂 vLLM 核心链路（第 7–10 周）

### 每周主线

- Week 7：追踪 OpenAI API、AsyncLLM 与 Engine Core 的请求链路
- Week 8：理解 scheduler、token budget、请求状态与 chunked prefill
- Week 9：理解 KV Cache Manager、block 生命周期与 prefix cache
- Week 10：追踪 GPU Worker、Model Runner、model forward 与 sampling

### 请求链路

```text
OpenAI API Server
    ↓ ZMQ
Engine Core
    ├── Scheduler
    ├── KV Cache Manager
    └── Request State
          ↓
GPU Worker
    ↓
Model Runner
    ↓
Attention / CUDA Graph / Model
```

### 阅读顺序

1. API 请求如何进入引擎
2. Engine Core 如何维护请求
3. Scheduler 每一步如何选择 token
4. KV Cache block 如何分配和回收
5. GPU Worker 如何执行模型
6. 输出如何流回客户端

不要按目录逐文件通读。每次围绕一个问题追踪代码：

- [ ] 新请求什么时候进入 running queue？
- [ ] preemption 在什么情况下发生？
- [ ] token budget 如何限制一个 scheduling step？
- [ ] KV Cache 不足时如何处理？
- [ ] prefix cache 命中后跳过了哪些计算？
- [ ] 一个请求取消后，资源如何释放？

### 动手任务

- [ ] 给 scheduler 增加调试 trace
- [ ] 记录每一步 scheduled tokens 和 waiting/running request 数
- [ ] 人为制造 KV Cache 压力
- [ ] 观察 preemption、排队和 cache eviction
- [ ] 用 timeline 展示请求状态变化
- [ ] 尝试提交一个小型 vLLM issue 修复、测试或文档 PR

### 阶段验收

- [ ] 能从 API 请求一路追踪到 GPU worker
- [ ] 能解释 API server、engine core 和 worker 的进程关系
- [ ] 能判断瓶颈位于 tokenization、queueing、scheduling、model execution 还是 output streaming

参考资料按首次收录规则维护：请求链路与架构入口见 [Week 7 references](week-07-references.md)。

## 第四阶段：推理性能专项（第 11–14 周）

执行计划：[Week 11](week-11-plan.md) / [资料](week-11-references.md)、[Week 12](week-12-plan.md) / [资料](week-12-references.md)、[Week 13](week-13-plan.md) / [资料](week-13-references.md)、[Week 14](week-14-plan.md) / [资料](week-14-references.md)。

### 每周主线

- Week 11：用 PyTorch Profiler 分解 framework/operator 瓶颈
- Week 12：用 Nsight Systems 重建 CPU–GPU timeline
- Week 13：用 Nsight Compute 深挖关键 kernel，并完成 prefix caching 专项
- Week 14：完成 chunked prefill、CUDA Graph 与 parallelism 的受控优化实验

### 工具

- [ ] PyTorch Profiler
- [ ] Nsight Systems
- [ ] Nsight Compute
- [ ] vLLM metrics
- [ ] 复用已有 Prometheus/Grafana 观测栈
- [ ] GPU utilization、memory bandwidth 和 kernel timeline

### 实验一：Prefix caching

- [ ] 构造大量复用 system prompt 的请求
- [ ] 比较启用前后的 TTFT、cache hit rate 和吞吐

### 实验二：Chunked prefill

- [ ] 构造长 prompt 与短请求混合负载
- [ ] 观察长 prompt 是否阻塞短请求
- [ ] 分析 TTFT 和 TPOT 的权衡

### 实验三：Quantization

- [ ] 比较 FP16/BF16 与 AWQ、GPTQ 或当前支持的低精度方案
- [ ] 同时记录质量、吞吐、延迟和显存变化

### 实验四：CUDA Graph

- [ ] 观察 CPU launch overhead
- [ ] 比较 decode latency

### 实验五：Parallelism

- [ ] 有多卡时至少完成一次 tensor parallel 实验
- [ ] 记录计算收益和通信开销

### 阶段产出

- [ ] 一份 profiling 报告
- [ ] 一个可复现的性能瓶颈案例
- [ ] 一次有数据支撑的优化
- [ ] 每个结论都记录硬件、模型、版本、参数和 workload

参考资料不在路线页重复 URL：profiling、tuning、paged attention 与 parallelism 的阅读顺序见 [Week 11 references](week-11-references.md)、[Week 12 references](week-12-references.md)、[Week 13 references](week-13-references.md) 和 [Week 14 references](week-14-references.md)。

## 第五阶段：多副本集成验证（第 15 周）

Kubernetes 基础已经掌握，本阶段不再学习 Pod、Deployment、Service、Probe、HPA 或 Prometheus 接入。直接用一周搭建最小多副本基线，为后续 Gateway API/GAIE 对照实验准备证据。

执行计划：[Week 15](week-15-plan.md) / [资料](week-15-references.md)。使用两个真实 GPU slots；区分 request-level round-robin 与 Service 的连接分发，并将 HPA 的副本变化和 GPU 成本一起报告。

### 部署任务

- [ ] 复用已有容器与 Kubernetes 模板部署 vLLM server
- [ ] 部署两个或更多 vLLM replicas
- [ ] 配置并验证 readiness、graceful shutdown 和请求排空
- [ ] 将各 replica 的推理指标接入已有观测栈
- [ ] 添加简单 round-robin gateway
- [ ] 运行固定副本与现有 HPA 的基线实验

### 需要证明的问题

- [ ] 单个 vLLM 实例只知道自己的队列和 KV Cache
- [ ] 普通负载均衡器不了解 prompt、token 数和 cache locality
- [ ] CPU/QPS 很难准确表示推理压力
- [ ] GPU Pod 启动和模型加载时间较长
- [ ] 扩容决策可能在新实例可用前已经过时
- [ ] round-robin 在长短请求混合或共享前缀负载下存在明显缺陷

### 一周产出

- [ ] 可重复部署的双副本 vLLM baseline
- [ ] round-robin 与现有 HPA 的可复现实验
- [ ] replica-level queue、TTFT、KV cache 与 GPU 指标证据
- [ ] 一份说明普通负载均衡和通用 HPA 局限的短报告

## 第六阶段：标准化推理网关与 EPP（第 16–18 周）

### 每周主线

- Week 16：Gateway API v1 L7 Baseline，用 `GatewayClass`、`Gateway` 和 `HTTPRoute` 建立可审计的 matching、traffic splitting、streaming 与请求归属基线（[计划](week-16-plan.md) / [资料](week-16-references.md)）。
- Week 17：GAIE `InferencePool` v1 与 Reference EPP，验证 `InferencePool`、reference EPP/ext-proc 数据路径、失败策略与 conformance 边界（[计划](week-17-plan.md) / [资料](week-17-references.md)）。
- Week 18：llm-d Router/EPP：Load-aware 与 Precise Prefix-aware Routing；固定双副本对比两种策略，并验证 locality、load、metric freshness 与 staleness 边界（[计划](week-18-plan.md) / [资料](week-18-references.md)）。

三周始终固定 vLLM image、模型、双副本资源和 workload。Week 16 只建立通用 L7 基线，Week 17 只引入标准 endpoint-selection contract，Week 18 只替换 EPP 实现；不在同一个 A/B 中同时改变 gateway、EPP、replica 数和模型配置。

### 标准请求路径与职责边界

```text
Client
  ↓
Gateway API Gateway + HTTPRoute
  ↓ backendRef
GAIE InferencePool
  ↓ endpointPickerRef
llm-d EPP
  ↓ selected endpoint
vLLM replica
```

- Gateway/HTTPRoute 负责 L7 listener、matching、policy attachment 和流量转发。
- `InferencePool` 描述同一模型服务的一组可选 endpoints；`v1` 稳定性只适用于对应 GAIE API，不等于所有推理扩展或云实现均已 GA。
- EPP 根据 endpoint 状态和推理信号做选择；llm-d EPP 是可替换的路由智能层，不是另一个 vLLM scheduler。
- vLLM 仍负责单实例内 batching、KV cache、scheduler、worker 和模型执行。
- Reference EPP 用于学习与 conformance 对照；生产型实验切换到 llm-d，且必须保留可回切配置。

### 阶段验收

- [ ] 能从 `HTTPRoute` status、`InferencePool`、EPP decision 一直追踪到具体 vLLM Pod。
- [ ] 能区分 Gateway API 核心 v1、GAIE `InferencePool` v1 和具体 gateway implementation 的支持范围。
- [ ] 完成 Service backend、reference EPP、llm-d load-aware 和 prefix-aware 的固定副本对照。
- [ ] 有 streaming、取消、Pod replacement、EPP unavailable/stale state 的请求级证据。
- [ ] 能解释 gateway、EPP、vLLM scheduler 和 autoscaler 的不同职责与时间尺度。

本阶段不在路线页重复外部书目；阅读顺序和首次收录来源见 [Week 16 references](week-16-references.md)、[Week 17 references](week-17-references.md) 和 [Week 18 references](week-18-references.md)。

## 第七阶段：声明式控制面、扩缩容与可移植 Capstone（第 19–22 周）

### 每周主线

- Week 19：KServe `LLMInferenceService` 声明式控制面与资源审计；固定 release，保存 alpha CRD schema、controller 生成对象、reconciliation、升级和回退证据（[计划](week-19-plan.md) / [资料](week-19-references.md)）。
- Week 20：HPA/KEDA 扩缩容与可观测性；固定 router，校准 metric contract，分解 cold-start timeline，比较 allocated 与 billed GPU-hours，WVA 仅作条件允许的选修对照（[计划](week-20-plan.md) / [资料](week-20-references.md)）。
- Week 21：云实现映射与可移植性验证；在 GKE 实跑一条 managed path，对 Azure、ACK、AWS 只做有官方来源的 API/capability mapping（[计划](week-21-plan.md) / [资料](week-21-references.md)）。
- Week 22：Capstone 的 held-out、故障、发布与 runbook；冻结 router/autoscaler，完成独立重复、最小消融、fault matrix、rollback 和最终演示（[计划](week-22-plan.md) / [资料](week-22-references.md)）。

KServe 是可选控制面，不取代 Week 16–18 的 portable data-plane contract。`LLMInferenceService` 当前仍是 alpha API；任何 `apiVersion`、生成资源和 upgrade 行为都以 Week 19 固定 release 的已安装 CRD 为准。每个 workload 只能有一个 replicas writer，不能让 HPA、KEDA、WVA 或其他 controller 同时修改副本数。

### Capstone 名称

**Portable Cloud-Native LLM Serving with Gateway API, GAIE, llm-d, and vLLM**

### 系统架构

```text
Workload Generator
        ↓
Gateway API: Gateway + HTTPRoute
        ↓
GAIE InferencePool
        ↓
llm-d EPP
├── load-aware selection
└── prefix-aware selection
        ↓
vLLM Replica Pool
├── Replica A
├── Replica B
└── Dynamically scaled replicas
        ↓
Prometheus / Logs / Traces → Dashboard → Experiment Report

HPA or KEDA ── metrics contract ──→ replica count

Optional control plane: KServe LLMInferenceService
```

### 实验矩阵

| 版本 | Endpoint selection | 扩缩容 | 目的 |
|---|---|---|---|
| Baseline A | Service / RR baseline | 固定副本 | Week 15–16 外部基线 |
| Baseline B | Reference EPP | 固定副本 | GAIE contract 基线 |
| Candidate A | llm-d load-aware | 固定副本 | 隔离 routing 影响 |
| Candidate B | llm-d prefix-aware | 固定副本 | 验证 locality/load trade-off |
| Candidate C | 冻结的 llm-d policy | HPA 或 KEDA | 隔离 scaling 影响 |

正式结论使用 uniform、long/short mixed、shared-prefix 和 burst/ramp 的 held-out traces，并包含失败、超时和取消请求。报告 P50/P95/P99 TTFT、TPOT、goodput、error rate、routing latency、cache hit、GPU 使用、扩缩容时间线，以及 allocated/billed GPU-hours。每个正式 cell 至少三个独立 run；样本不足时只作探索性描述。

### 云与 API 边界

- `InferencePool` v1 是稳定 API；这不代表所有 GAIE 周边资源、gateway implementation 或云产品都处于同一稳定级别。
- GKE 是主路线唯一要求实跑的 managed-cloud 路径。Azure、ACK、AWS 的产出是官方文档支持的 mapping，除非报告明确记录真实部署。
- provider tutorial、reference architecture、preview feature、conformance result 和 managed GA product 必须分别标注，不能互相替代。
- 不做市场份额、采用率或“所有云厂商都使用某实现”的推断。

### 阶段验收

- [ ] 保存标准对象、KServe 生成对象和 provider-specific 资源的 ownership/compatibility matrix。
- [ ] HPA/KEDA 至少完成 burst、ramp、降载和 metric outage 四类时间线，且 replicas writer 唯一。
- [ ] GKE 完成真实 streaming smoke 与请求归属；其他云 mapping 有明确来源和未验证边界。
- [ ] Capstone 完成 routing 与 scaling 的最小消融，不把同时变化的控制环归因给单一组件。
- [ ] EPP unavailable/stale、Pod drain、cold start、gateway restart 和 rollout 均有故障/恢复证据。
- [ ] 最终结论允许“没有改善”，不把单次 run、厂商数字或计划中的 X/Y/Z 当作本项目结果。

本阶段的外部资料只在 weekly references 首次编号：见 [Week 19 references](week-19-references.md)、[Week 20 references](week-20-references.md)、[Week 21 references](week-21-references.md) 和 [Week 22 references](week-22-references.md)。

## 第八阶段：多节点 vLLM 与 Kubernetes 运维交接（第 23–24 周）

### 每周主线

- Week 23：vLLM native multiprocessing 多节点并行与 Runtime Contract；冻结 TP/PP/DP/EP、进程启动、rank mapping、通信和 shutdown 边界，2 nodes × 1 L4 只做功能 smoke，高速网络/多卡满足门槛后才做性能结论（[计划](week-23-plan.md) / [资料](week-23-references.md)）。
- Week 24：LeaderWorkerSet + Kueue 的 gang admission、拓扑调度、故障恢复与最终 multi-node operations handoff；验证 LWS group lifecycle/failure semantics、Kueue all-or-nothing admission 和 topology-aware scheduling，并交付可重复的部署、恢复、升级、观测和成本 runbook（[计划](week-24-plan.md) / [资料](week-24-references.md)）。

### 分层模型

```text
vLLM native multiprocessing
└── 单模型副本内的 TP / PP / DP / EP、进程启动、rank mapping 与通信

Kubernetes workload path
├── LeaderWorkerSet：leader + workers 组成复制单元并定义 group lifecycle
└── Kueue：all-or-nothing admission、gang 与 topology-aware scheduling
```

LWS 不替代 vLLM 的 distributed runtime；它负责把 leader 与 workers 建模为一个 Kubernetes workload group。Kueue 在这一层提供 admission 与 topology placement。Week 23 冻结进程级 runtime contract，Week 24 只改变 Kubernetes workload 与调度层，并把验证结果收敛为最终 multi-node operations handoff。

### 硬件与结论门槛

- 最低功能验证：两个同区节点、每节点 1×L4，可做 PP/跨节点 TP smoke、DP、operator lifecycle、gang 和 failure；不能宣称生产级 collective 性能。
- 更完整教学矩阵：每节点至少两张同型号 GPU，可比较单节点 TP、跨节点 PP/TP 和 DP，但普通 L4/TCP 仍以 runtime/orchestration 结论为主。
- 有意义的 TP/EP 性能实验需要匹配的多 GPU 节点、已知的 NVLink/NVSwitch/网络拓扑、高带宽节点间互联和通过的 collective benchmark；EP 还必须使用受支持的 MoE 模型。
- 硬件不足时保留 manifest、smoke、调度与错误证据，将性能 cell 标记 `blocked`/`deferred`，不外推到未测试硬件。

### 阶段验收

- [ ] 能区分模型因单卡放不下而分片、为吞吐复制 engine，以及 Kubernetes 如何把这些进程组织成工作负载。
- [ ] vLLM native multiprocessing 有可复现的 launch、rank mapping、通信、health check 与 shutdown 证据。
- [ ] LWS + Kueue 有 group identity、all-or-nothing admission、topology 和 group recovery 证据。
- [ ] Week 24 在固定硬件、镜像和 workload 下完成 scheduling、failure、upgrade、observability 与成本验证，而非只比较吞吐。
- [ ] 最终 handoff 包含部署、排障、节点故障恢复、受控升级、回滚和停止计费资源的可执行 runbook。
- [ ] 职责边界可审计：vLLM native multiprocessing 管理模型进程与通信，LWS 管理 workload group lifecycle，Kueue 管理 admission 与 topology placement。

本阶段资料入口见 [Week 23 references](week-23-references.md) 和 [Week 24 references](week-24-references.md)。

## 推荐仓库结构

```text
adaptive-llm-serving/
├── README.md
├── configs/
│   └── weekXX-*.yaml
├── docs/
│   ├── architecture.md
│   ├── vllm-request-lifecycle.md
│   ├── experiment-methodology.md
│   ├── cloud-portability.md
│   ├── multi-node-operations.md
│   └── results.md
├── deploy/
│   ├── vllm/
│   ├── gateway-api/
│   ├── inference-pool/
│   ├── llm-d/
│   ├── kserve/
│   ├── autoscaling/
│   ├── lws/
│   ├── kueue/
│   ├── providers/
│   └── monitoring/
├── benchmark/
│   ├── workloads/
│   ├── runner/
│   └── analysis/
├── dashboards/
├── reports/
├── results/
├── scripts/
├── tests/
└── Makefile
```

所有实验尽量变成一条命令：

```bash
make deploy
make benchmark SCENARIO=prefix-burst POLICY=service-rr
make benchmark SCENARIO=prefix-burst POLICY=llm-d-prefix
make report
```

## 最终交付物

- [ ] 5 分钟内可以理解的 README
- [ ] 一张系统架构图
- [ ] 一张请求生命周期图
- [ ] 可重复执行且固定版本的 Gateway API、GAIE、llm-d 与 vLLM 部署脚本
- [ ] `LLMInferenceService` alpha schema、生成资源和 reconciliation 审计
- [ ] 可重复执行的 benchmark
- [ ] 关联 Gateway、EPP、vLLM 与 autoscaler 的 dashboard
- [ ] Service/RR、reference EPP、llm-d routing 与选定 autoscaler 的最小消融
- [ ] GKE 实跑证据与 Azure/ACK/AWS capability mapping
- [ ] vLLM native multiprocessing 多节点证据与 LWS + Kueue operations handoff
- [ ] profiling 截图或 timeline
- [ ] 失败、blocked/deferred 实验和设计取舍记录
- [ ] deployment、rollback 与故障恢复 runbook
- [ ] 3–5 分钟演示视频
- [ ] 一篇技术文章
- [ ] 最好完成一个 vLLM、Gateway API/GAIE、llm-d、KServe、LWS 或 Kueue 上游贡献

## 简历描述模板

> Built a portable cloud-native LLM serving platform with vLLM, Kubernetes Gateway API, GAIE InferencePool, and llm-d EPP. Evaluated load- and prefix-aware routing plus HPA/KEDA autoscaling under bursty, long-context, and prefix-heavy workloads using P99 TTFT, goodput, routing latency, KV-cache hit rate, and allocated/billed GPU-hours; validated the managed path on GKE and documented provider mappings.

多节点实验完成后可增加：

> Ran multi-node vLLM with native multiprocessing, then operationalized it with LeaderWorkerSet and Kueue, documenting gang admission, topology placement, failure recovery, controlled upgrades, rollback, observability, and cost in a reproducible operations handoff.

实验完成后，补上实际提升数字和实验条件。

## 执行原则

1. 不花两个月从零复刻 vLLM；Mini Engine 只用于建立性能直觉。
2. 不把最终项目做成纯 Kubernetes 部署；必须包含推理指标、请求归属、策略、故障和对照实验。
3. 先验证标准 `Gateway`/`HTTPRoute`/`InferencePool` contract，再比较实现；不把实现私有 API 当作云通用基线。
4. 每个性能结论必须记录硬件、模型、软件版本、参数和 workload。
5. 先建立正确且可复现的 baseline，再进行优化。
6. 每个实验只改变一个主要控制面变量；routing、autoscaling 和多节点 workload lifecycle 分阶段验证。
7. API version、conformance、provider preview/GA 和实际运行证据分别记录，不推断市场份额或普遍采用率。
8. GPU 或网络条件不满足时标记 blocked/deferred，不用 CPU smoke 或普通 L4/TCP 外推性能。
9. 优先提交小而清晰的上游贡献，证明能够阅读并改动真实推理系统。

## 时间调整

- 每周约 5 小时：将 24 周路线延长到约 11–12 个月；保持前置关系，不把两个 GPU-heavy milestone 硬塞进同一周。
- 当前精简版已假设熟悉 Prometheus 与 Kubernetes：第五阶段从 3 周压缩到 1 周，Week 5 只保留 vLLM metric contract 与实验对齐。
- GPU quota 暂缺：继续做源码阅读、manifest、schema/object graph、离线分析和 provider mapping；任何性能 cell 保持 blocked，拿到相同 GPU 条件后再补跑。
- 高速多节点资源暂缺：Week 23–24 先完成 correctness、调度、failure 和 operations handoff，TP/EP collective 性能延后，不阻塞前 22 周 capstone 收尾。
- 目标偏 CUDA/Kernel：增加 Triton、CUDA 和算子 profiling，减少 provider mapping 深度，但保留标准网关 contract。
- 目标偏 AI Infra/Serving：保持当前比重，重点打磨 EPP、扩缩容、可观测性、故障实验和 LWS + Kueue 运维证据。
