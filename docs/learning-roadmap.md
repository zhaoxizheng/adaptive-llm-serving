# Learning Roadmap: 从 vLLM 到 Cloud-Native LLM Serving

> 目标：用 20 周、通常每周约 10–12 小时，从理解单机 LLM 推理逐步过渡到云厂商通用的 Kubernetes 推理服务架构，并完成一个以 vLLM、Gateway API、Gateway API Inference Extension（GAIE）和 llm-d 为主线的可复现项目。Prometheus 与 Kubernetes 作为已掌握的基础设施直接使用，不再安排基础学习。

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
验证扩缩容、指标故障、冷启动与成本边界
```

最终项目：

> 构建一个基于 vLLM + Kubernetes Gateway API + GAIE `InferencePool` + llm-d EPP 的自适应 LLM Serving 平台，在突发流量、长短请求混合和共享前缀三类负载下，对路由、扩缩容、受控故障语义与资源成本进行可复现实验。每个 vLLM replica 必须完整运行在一台服务器内，可使用一张或多张同节点 GPU；HPA/KEDA 是主扩缩容路径，KServe `LLMInferenceService` 作为可选声明式控制面对照，不是主数据面的前提。

这是路线唯一的生产模型执行形态：集群可以把多个独立 replicas 放在不同节点，但绝不把同一个 replica 的 GPU 或 TP ranks 拆到多个节点。

默认前提：有普通后端开发经验，了解 Python 和 Linux。开发设备是 36 GB 内存的 Mac M3 Pro，日常内存占用可能达到约 30 GB，因此从第一阶段开始就使用按小时计费的云端 NVIDIA GPU；本地 Mac 只负责写代码、Git、查看实验结果、分析数据和撰写文档，不在本地加载模型或运行正式 benchmark。

## 开发与 GPU 环境策略

### 为什么从一开始就使用云端 GPU

- vLLM 的主要学习和性能路径围绕 Linux、NVIDIA CUDA 展开，Apple Silicon/MPS 不适合作为这条路线的基准环境。
- 本地统一内存已经长期处于高占用状态，继续加载模型容易引发 swap、系统卡顿和不可重复的性能结果。
- 从第一天就在 CUDA 环境运行，可以避免前期代码在 MPS/CPU 上可用、迁移到 vLLM 和 CUDA 时又重新适配。
- 后续的 Triton、Nsight、同机多卡 TP 和多副本路由实验本来就需要 NVIDIA GPU 或 Linux 集群。

### 环境分工

| 环境 | 负责内容 | 不负责内容 |
|---|---|---|
| Mac M3 Pro | 编辑代码、Git、SSH、阅读源码、画图、分析下载后的指标、写报告 | 加载模型、运行 vLLM、正式性能测试 |
| 单卡云 GPU | Mini Inference Lab、vLLM 单实例、profiling、参数调优 | 多副本和多卡结论 |
| 多卡云主机或 GPU Kubernetes | 同机 tensor parallel、多副本路由、Gateway/GAIE、扩缩容与故障实验 | 日常编码和长期空闲开发 |

### 分阶段 GPU 建议

- 第 1–4 阶段：默认使用 GCP Compute Engine `g2-standard-4` Spot（1×NVIDIA L4 24 GB、4 vCPU、16 GiB 内存），优先从 `us-central1-a` 尝试。它足够运行小模型、vLLM 和大多数单卡实验。若该区没有 Spot 容量，可换同区域的 G2 可用区，或等待后重试。
- tensor parallel 实验：短租一台至少双卡且卡间通信拓扑明确的机器。所有对比应固定 GPU 型号和数量。
- Kubernetes serving 阶段：迁移到 GKE。一个 replica 固定为 `1 Pod / 1 node / G GPUs / TP=G`；双副本实验需要 `2 × G` 张 GPU，且每组 `G` 张 GPU 必须能在同一节点分配。纯 CPU 集群只用于验证 CRD、控制器和 reconciliation，不用于性能结论。
- 路由和 autoscaling 只改变完整 replica 的选择与数量，不改变运行中 replica 的 `G` 或 TP degree。
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
6. GCP Spot 适合单卡 benchmark，但脚本必须按 case 原子落盘并可断点续跑；同机多卡 TP 和受控多副本对比优先使用稳定按需容量。

## 第一阶段：建立推理性能直觉（第 1–3 周）

### 学习目标

- [ ] 理解 Transformer decoder 的基本数据流
- [ ] 理解 prefill 和 decode 的区别
- [ ] 掌握 KV Cache 的作用及大小估算方法
- [ ] 理解 batch size、sequence length 对显存和延迟的影响
- [ ] 理解 compute-bound 和 memory-bound
- [ ] 掌握 TTFT、TPOT、ITL、E2E latency、throughput 和 goodput
- [ ] 区分同机 tensor parallel 与独立 replica 横向扩容

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

第 5–6 周已提供实现与中文代码导读：[Week 5：观测、SLO 与容量](week-05-code-walkthrough.md)、
[Week 6：量化与参数调优](week-06-code-walkthrough.md)。代码生成不代表 GPU 实验已完成；
容量与 operating point 需从各周保存的证据得出。

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

第 7–8 周已提供固定 `vLLM 0.10.2` revision 的 trace patch、运行脚本和中文导读：
[Week 7：请求链路](week-07-code-walkthrough.md)、[Week 8：Scheduler](week-08-code-walkthrough.md)。
源码地图与 patch 可离线核对；runtime trace 和机制验证在单张 L4 上完成。

第 9–10 周也已提供代码、默认关闭的 trace patch 和中文导读：
[Week 9：KV block 生命周期](week-09-code-walkthrough.md)、
[Week 10：GPU Worker / Model Runner](week-10-code-walkthrough.md)。
Patch 顺序、源码环境和运行前置条件见 [第 9–12 周运行约定](week-09-12-runbook.md)。

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

第 11–12 周已有成对 baseline/profile runner、离线摘要和中文导读：
[Week 11：PyTorch Profiler](week-11-code-walkthrough.md)、
[Week 12：Nsight Systems](week-12-code-walkthrough.md)。
第 13–14 周已提供实现与中文导读：[Week 13：Kernel/APC](week-13-code-walkthrough.md)、
[Week 14：优化与 TP](week-14-code-walkthrough.md)。代码生成和 CPU 测试不代表已完成
GPU capture；报告需用真实 trace、overhead、shape 检查和资源停止证据填写。

### 每周主线

- Week 11：用 PyTorch Profiler 分解 framework/operator 瓶颈
- Week 12：用 Nsight Systems 重建 CPU–GPU timeline
- Week 13：用 Nsight Compute 深挖关键 kernel，并完成 prefix caching 专项
- Week 14：完成 chunked prefill、CUDA Graph 与单机多卡 TP 的受控优化实验

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

- [ ] 在一台多卡服务器上完成 `1 replica = 1 Pod = G GPUs = TP G` 实验
- [ ] 保存 `nvidia.com/gpu` allocation、visible GPUs、local rank mapping 与节点内 topology
- [ ] Week 14 完成 `1 × TP=1` 与同机 `1 × TP=2` 的容量/通信 smoke，并冻结 replica shape
- [ ] Week 15 再以相同两卡总预算对比 `1 × TP=2` 与 `2 × TP=1 replicas`，记录 latency、throughput、故障隔离和 GPU-seconds/request

### 阶段产出

- [ ] 一份 profiling 报告
- [ ] 一个可复现的性能瓶颈案例
- [ ] 一次有数据支撑的优化
- [ ] 每个结论都记录硬件、模型、版本、参数和 workload

参考资料不在路线页重复 URL：profiling、tuning、paged attention 与 parallelism 的阅读顺序见 [Week 11 references](week-11-references.md)、[Week 12 references](week-12-references.md)、[Week 13 references](week-13-references.md) 和 [Week 14 references](week-14-references.md)。

## 第五阶段：多副本集成验证（第 15 周）

Kubernetes 基础已经掌握，本阶段不再学习 Pod、Deployment、Service、Probe、HPA 或 Prometheus 接入。直接用一周搭建最小多副本基线，为后续 Gateway API/GAIE 对照实验准备证据。

执行计划：[Week 15](week-15-plan.md) / [资料](week-15-references.md)。使用两个完整 replica slots；若每个 replica 使用 `G` 张同节点 GPU，则共需 `2 × G` 张 GPU。区分 request-level round-robin 与 Service 的连接分发，并将 HPA 的副本变化和 GPU 成本一起报告。

实现与中文导读：[Week 15：RR 与生命周期](week-15-code-walkthrough.md)。
四周的环境锁、运行模式与验收边界见 [第 13–16 周运行约定](week-13-16-runbook.md)。

### 部署任务

- [ ] 创建容器与 Kubernetes 模板；实现后复用它们部署 vLLM server
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
- Week 16 实现与中文导读：[L7 路由代码](week-16-code-walkthrough.md) / [Gateway contract](gateway-api-contract.md)；真实 controller 与 GPU 验收仍需执行。
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
- vLLM 仍负责单个逻辑 replica 内的 batching、KV cache、scheduler、worker 和模型执行；该 replica 可在同节点使用多卡 TP。
- Reference EPP 用于学习与 conformance 对照；生产型实验切换到 llm-d，且必须保留可回切配置。

### 阶段验收

- [ ] 能从 `HTTPRoute` status、`InferencePool`、EPP decision 一直追踪到具体 vLLM Pod。
- [ ] 能区分 Gateway API 核心 v1、GAIE `InferencePool` v1 和具体 gateway implementation 的支持范围。
- [ ] 完成 Service backend、reference EPP、llm-d load-aware 和 prefix-aware 的固定副本对照。
- [ ] 有 streaming、取消、Pod replacement、EPP unavailable/stale state 的请求级证据。
- [ ] 能解释 gateway、EPP、vLLM scheduler 和 autoscaler 的不同职责与时间尺度。

本阶段不在路线页重复外部书目；阅读顺序和首次收录来源见 [Week 16 references](week-16-references.md)、[Week 17 references](week-17-references.md) 和 [Week 18 references](week-18-references.md)。

## 第七阶段：声明式控制面与扩缩容收尾（第 19–20 周）

### 每周主线

- Week 19：KServe `LLMInferenceService` 声明式控制面与资源审计；固定 release，保存 alpha CRD schema、controller 生成对象、reconciliation、升级和回退证据（[计划](week-19-plan.md) / [资料](week-19-references.md)）。
- Week 20：HPA/KEDA 扩缩容与可观测性；固定 router，校准 metric contract，分解 cold-start timeline，并比较 allocated 与 billed GPU-hours（[计划](week-20-plan.md) / [资料](week-20-references.md)）。

KServe 是可选控制面，不取代 Week 16–18 的 portable data-plane contract。`LLMInferenceService` 当前仍是 alpha API；任何 `apiVersion`、生成资源和 upgrade 行为都以 Week 19 固定 release 的已安装 CRD 为准。Week 20 的通用 autoscaling 实验使用 vLLM `Deployment` 作为共同 `/scale` target，并且只能选择独立 HPA 或 KEDA `ScaledObject` 其中一条扩缩容路径，不能让两个 controller 或 GitOps/manual loop 同时修改副本数。

### 最终项目名称

**Cloud-Native LLM Serving with Gateway API, GAIE, llm-d, and vLLM**

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
├── Replica A：1 Pod / 1 node / G GPUs / TP=G
├── Replica B：1 Pod / 1 node / G GPUs / TP=G
└── Dynamically scaled complete replicas（TP 固定）
        ↓
Prometheus / Logs / Traces → Dashboard → Experiment Report

Prometheus Adapter → independent HPA ─┐
                                      ├──→ vLLM Deployment /scale
Prometheus → KEDA → generated HPA ────┘

Optional control plane: KServe LLMInferenceService
```

### 累计实验矩阵

| 版本 | Endpoint selection | 扩缩容 | 目的 |
|---|---|---|---|
| Baseline A | Service / RR baseline | 固定副本 | Week 15–16 外部基线 |
| Baseline B | Reference EPP | 固定副本 | GAIE contract 基线 |
| Candidate A | llm-d load-aware | 固定副本 | 隔离 routing 影响 |
| Candidate B | llm-d prefix-aware | 固定副本 | 验证 locality/load trade-off |
| Candidate C | 冻结的 llm-d policy | HPA 或 KEDA | 隔离 scaling 影响 |

矩阵汇总 Week 15–20 已分别定义的实验，不新增一轮最终集成测试。各周继续按自己的固定 workload、重复数和失败分母报告 P50/P95/P99 TTFT、TPOT、goodput、error rate、routing latency、cache hit、GPU 使用、扩缩容时间线，以及 allocated/billed GPU-hours；样本不足时只作探索性描述。

### API 与实现边界

- `InferencePool` v1 是稳定 API；这不代表所有 GAIE 周边资源、gateway implementation 或云产品都处于同一稳定级别。
- API 规范、controller 实现、conformance result 与本项目的运行证据必须分别标注，不能互相替代。
- 不从文档、实现列表或单一实验环境推断市场份额和普遍采用率。

### 阶段验收

- [ ] 保存标准对象、KServe 生成对象和所选 gateway 实现的 ownership/compatibility matrix。
- [ ] HPA/KEDA 至少完成 burst、ramp、降载和 metric outage 四类时间线，且 replicas writer 唯一。
- [ ] 汇总 Week 18 routing 与 Week 20 scaling 的独立对照，不把同时变化的控制环归因给单一组件。
- [ ] EPP unavailable/stale、Pod drain、cold start 和 metric outage 均有故障/恢复证据。
- [ ] 最终结论允许“没有改善”，不把单次 run、厂商数字或计划中的 X/Y/Z 当作本项目结果。

本阶段的外部资料只在 weekly references 首次编号：见 [Week 19 references](week-19-references.md) 和 [Week 20 references](week-20-references.md)。

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
│   └── results.md
├── deploy/
│   ├── vllm/
│   ├── gateway-api/
│   ├── inference-pool/
│   ├── llm-d/
│   ├── kserve/
│   ├── autoscaling/
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
- [ ] 单机多卡 vLLM replica 的 TP、资源放置、rank mapping 与成本证据
- [ ] profiling 截图或 timeline
- [ ] 失败、blocked/deferred 实验和设计取舍记录
- [ ] 一篇技术文章
- [ ] 最好完成一个 vLLM、Gateway API/GAIE、llm-d 或 KServe 上游贡献

## 简历描述模板

> Built a cloud-native LLM serving platform with vLLM, Kubernetes Gateway API, GAIE InferencePool, and llm-d EPP. Evaluated load- and prefix-aware routing plus HPA/KEDA autoscaling under bursty, long-context, and prefix-heavy workloads using P99 TTFT, goodput, routing latency, KV-cache hit rate, and allocated/billed GPU-hours.

实验完成后，补上实际提升数字和实验条件。

## 执行原则

1. 不花两个月从零复刻 vLLM；Mini Engine 只用于建立性能直觉。
2. 不把最终项目做成纯 Kubernetes 部署；必须包含推理指标、请求归属、策略、故障和对照实验。
3. 先验证标准 `Gateway`/`HTTPRoute`/`InferencePool` contract，再比较实现；不把实现私有 API 当作云通用基线。
4. 每个性能结论必须记录硬件、模型、软件版本、参数和 workload。
5. 先建立正确且可复现的 baseline，再进行优化。
6. 每个实验只改变一个主要变量；单机 TP、routing 和 autoscaling 分阶段验证。
7. API version、conformance、实现能力和实际运行证据分别记录，不推断市场份额或普遍采用率。
8. GPU 或网络条件不满足时标记 blocked/deferred，不用 CPU smoke 或普通 L4/TCP 外推性能。
9. 优先提交小而清晰的上游贡献，证明能够阅读并改动真实推理系统。

## 时间调整

- 每周约 5 小时：将 20 周路线延长到约 9–11 个月；保持前置关系，不把两个 GPU-heavy milestone 硬塞进同一周。
- 当前精简版已假设熟悉 Prometheus 与 Kubernetes：第五阶段从 3 周压缩到 1 周，Week 5 只保留 vLLM metric contract 与实验对齐。
- GPU quota 暂缺：继续做源码阅读、manifest、schema/object graph 和离线分析；任何性能 cell 保持 blocked，拿到相同 GPU 条件后再补跑。
- 同机双卡资源暂缺：保留 Week 14 的 deployment/TP 实验设计并标记 deferred；不以不同 GPU 或不可比资源拼接结果替代。
- 目标偏 CUDA/Kernel：增加 Triton、CUDA 和算子 profiling，减少控制面审计深度，但保留标准网关 contract。
- 目标偏 AI Infra/Serving：保持当前比重，重点打磨同机 TP、EPP、扩缩容、可观测性和故障实验。
