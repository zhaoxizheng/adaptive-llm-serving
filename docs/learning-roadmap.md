# Learning Roadmap: 从 vLLM 到 AIBrix

> 目标：用 22 周、通常每周约 10–12 小时，从理解单机 LLM 推理逐步过渡到集群级推理服务，并完成一个基于 vLLM、Kubernetes 和 AIBrix 的可复现项目。Prometheus 与 Kubernetes 作为已掌握的基础设施直接使用，不再安排基础学习。

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
加入路由、监控、扩缩容
    ↓
用 AIBrix 实现集群级推理优化
```

最终项目：

> 构建一个基于 vLLM + Kubernetes + AIBrix 的自适应 LLM Serving 平台，在突发流量、长短请求混合和共享前缀三类负载下，实现 SLO-aware 路由与扩缩容，并通过完整 benchmark 证明其相对基线的收益。

默认前提：有普通后端开发经验，了解 Python 和 Linux。开发设备是 36 GB 内存的 Mac M3 Pro，日常内存占用可能达到约 30 GB，因此从第一阶段开始就使用按小时计费的云端 NVIDIA GPU；本地 Mac 只负责写代码、Git、查看实验结果、分析数据和撰写文档，不在本地加载模型或运行正式 benchmark。

## 开发与 GPU 环境策略

### 为什么从一开始就使用云端 GPU

- vLLM 的主要学习和性能路径围绕 Linux、NVIDIA CUDA 展开，Apple Silicon/MPS 不适合作为这条路线的基准环境。
- 本地统一内存已经长期处于高占用状态，继续加载模型容易引发 swap、系统卡顿和不可重复的性能结果。
- 从第一天就在 CUDA 环境运行，可以避免前期代码在 MPS/CPU 上可用、迁移到 vLLM 和 CUDA 时又重新适配。
- 后续的 Triton、Nsight、多卡并行和 AIBrix 实验本来就需要 NVIDIA GPU 或 Linux 集群。

### 环境分工

| 环境 | 负责内容 | 不负责内容 |
|---|---|---|
| Mac M3 Pro | 编辑代码、Git、SSH、阅读源码、画图、分析下载后的指标、写报告 | 加载模型、运行 vLLM、正式性能测试 |
| 单卡云 GPU | Mini Inference Lab、vLLM 单实例、profiling、参数调优 | 多副本和多卡结论 |
| 多卡云主机或 GPU Kubernetes | tensor parallel、多副本路由、AIBrix、扩缩容与故障实验 | 日常编码和长期空闲开发 |

### 分阶段 GPU 建议

- 第 1–4 阶段：默认使用 GCP Compute Engine `g2-standard-4` Spot（1×NVIDIA L4 24 GB、4 vCPU、16 GiB 内存），优先从 `us-central1-a` 尝试。它足够运行小模型、vLLM 和大多数单卡实验。若该区没有 Spot 容量，可换同区域的 G2 可用区，或等待后重试。
- tensor parallel 实验：短租一台至少双卡且卡间通信拓扑明确的机器。所有对比应固定 GPU 型号和数量。
- Kubernetes/AIBrix 阶段：迁移到 GKE，使用至少两个可调度 GPU 实例或一台多 GPU 节点，确保能真实比较多副本路由。纯 CPU 集群只用于验证控制面安装，不用于性能结论。
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

参考资料：[vLLM Architecture Overview](https://docs.vllm.ai/en/latest/design/arch_overview/)

## 第四阶段：推理性能专项（第 11–14 周）

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

参考资料：

- [vLLM Profiling](https://docs.vllm.ai/en/latest/contributing/profiling/)
- [vLLM Optimization and Tuning](https://docs.vllm.ai/en/latest/configuration/optimization/)
- [vLLM Paged Attention](https://docs.vllm.ai/en/latest/design/paged_attention/)

## 第五阶段：多副本集成验证（第 15 周）

Kubernetes 基础已经掌握，本阶段不再学习 Pod、Deployment、Service、Probe、HPA 或 Prometheus 接入。直接用一周搭建最小多副本基线，为 AIBrix 对照实验准备证据。

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

## 第六阶段：学习 AIBrix（第 16–18 周）

### 架构边界

```text
控制面
├── 模型与适配器管理
├── inference-aware autoscaling
├── GPU optimizer
└── runtime/controller

数据面
├── Request Router
├── rate limit / fairness / isolation
└── distributed KV cache
```

### 学习顺序

1. Architecture：组件职责和请求路径
2. Gateway：请求如何路由到模型实例
3. Benchmark：如何生成可控工作负载
4. Metrics：AIBrix 可以观察哪些推理指标
5. Autoscaler：指标如何转化为副本数
6. KV Cache：cache locality 如何影响路由
7. Model management：模型部署和运行时生命周期

### 小实验

- [ ] round-robin 与 least-request 路由对比
- [ ] CPU-based HPA 与 inference-metric autoscaling 对比
- [ ] 随机路由与 prefix/cache-aware 路由对比

### 阶段验收

- [ ] 能区分哪些优化应放在 vLLM，哪些应放在 AIBrix
- [ ] 能解释路由、scheduler、autoscaler 三者不同的时间尺度
- [ ] 能从 gateway 一直追踪到具体 vLLM Pod
- [ ] 能解释为什么只依赖 GPU utilization 扩缩容可能不稳定

参考资料：

- [AIBrix Architecture](https://aibrix.readthedocs.io/latest/designs/architecture.html)
- [Gateway Routing](https://aibrix.readthedocs.io/latest/features/gateway-plugins.html)
- [Autoscaling](https://aibrix.readthedocs.io/latest/features/autoscaling/autoscaling.html)
- [Benchmark and Workload Generator](https://aibrix.readthedocs.io/latest/features/benchmark-and-generator.html)
- [KV Cache Events Synchronization](https://aibrix.readthedocs.io/latest/features/kv-event-sync.html)

## 第七阶段：最终项目（第 19–22 周）

### 项目名称

**Adaptive LLM Serving Platform: SLO-aware Routing and Autoscaling for vLLM on AIBrix**

### 项目问题

在以下混合流量中，如何保持 P99 TTFT SLO，同时提高 GPU 利用率？

- 短对话请求
- 长上下文请求
- 突发流量
- 大量共享 system prompt
- 两个具有不同优先级的租户

### 系统架构

```text
Workload Generator
        ↓
AIBrix Gateway
        ↓
Custom Routing Policy
├── queue/load-aware score
├── prefix/cache-affinity score
└── tenant/SLO priority
        ↓
vLLM Replica Pool
├── Replica A
├── Replica B
└── Dynamically scaled replicas
        ↓
Prometheus → Grafana → Experiment Report

AIBrix Autoscaler
        ↑
queue depth / TTFT / KV usage / token rate
```

### 核心实现：SLO-aware + prefix-aware routing

不要重新实现完整 AIBrix。只负责一个窄而完整的创新点。

给每个 replica 计算 routing score：

```text
score =
  alpha × normalized_queue_load
+ beta  × estimated_completion_time
- gamma × prefix_cache_affinity
+ delta × tenant_priority_penalty
```

第一版使用：

- running request 数
- waiting request 数
- 估计 prompt token 数
- KV Cache 使用率
- prefix hash 是否命中

随后加入 autoscaling：

- [ ] 扩容信号：预测 P95/P99 TTFT 将超过 SLO
- [ ] 缩容信号：持续低负载、无活跃请求且缓存价值较低
- [ ] 添加 cooldown，避免扩缩容震荡
- [ ] 将冷启动和模型加载时间纳入判断

### 实验矩阵

| 版本 | 路由 | 扩缩容 |
|---|---|---|
| Baseline A | Round-robin | 固定副本 |
| Baseline B | Least-request | 固定副本 |
| Baseline C | Least-request | CPU/GPU HPA |
| Proposed | SLO + prefix-aware | inference-aware |

### 测试场景

- [ ] 恒定流量
- [ ] 突发流量
- [ ] Zipf 分布的共享前缀
- [ ] 长短请求混合
- [ ] 多租户优先级
- [ ] 单 Pod 故障

### 核心指标

- P50/P95/P99 TTFT
- P50/P99 TPOT
- request latency
- request throughput
- token throughput
- SLO attainment / goodput
- KV Cache hit rate
- GPU utilization
- 显存利用率
- 扩容反应时间
- 单位百万 token 的 GPU 成本

最终结论应该是有条件、有基线、有数字的陈述，例如：

> 在相同 GPU 数量下，prefix-aware routing 将共享前缀负载的 P99 TTFT 降低 X%；在 burst workload 下，SLO-aware autoscaling 将 SLO attainment 从 Y% 提升至 Z%，代价是 GPU-hours 增加 N%。

X、Y、Z、N 必须来自可复现实验，不能预先设定。

## 推荐仓库结构

```text
adaptive-llm-serving/
├── README.md
├── docs/
│   ├── architecture.md
│   ├── vllm-request-lifecycle.md
│   ├── experiment-methodology.md
│   └── results.md
├── deploy/
│   ├── kind-or-k3s/
│   ├── vllm/
│   ├── aibrix/
│   └── monitoring/
├── router/
│   ├── policy/
│   ├── metrics/
│   └── tests/
├── autoscaler/
│   ├── controller/
│   ├── predictor/
│   └── tests/
├── benchmark/
│   ├── workloads/
│   ├── runner/
│   └── analysis/
├── dashboards/
├── scripts/
└── Makefile
```

所有实验尽量变成一条命令：

```bash
make deploy
make benchmark SCENARIO=prefix-burst POLICY=round-robin
make benchmark SCENARIO=prefix-burst POLICY=slo-prefix
make report
```

## 最终交付物

- [ ] 5 分钟内可以理解的 README
- [ ] 一张系统架构图
- [ ] 一张请求生命周期图
- [ ] 可重复执行的部署脚本
- [ ] 可重复执行的 benchmark
- [ ] Grafana dashboard
- [ ] 至少三组基线对比
- [ ] profiling 截图或 timeline
- [ ] 失败实验和设计取舍记录
- [ ] 3–5 分钟演示视频
- [ ] 一篇技术文章
- [ ] 最好完成一个 vLLM 或 AIBrix 上游 PR

## 简历描述模板

> Built an adaptive LLM serving platform using vLLM, Kubernetes, and AIBrix. Implemented SLO- and prefix-aware request routing with inference-metric autoscaling, and evaluated it under bursty, long-context, and prefix-heavy workloads using P99 TTFT, goodput, KV-cache hit rate, and GPU-hours.

实验完成后，补上实际提升数字和实验条件。

## 执行原则

1. 不花两个月从零复刻 vLLM；Mini Engine 只用于建立性能直觉。
2. 不把最终项目做成纯 Kubernetes 部署；必须包含推理指标、策略和对照实验。
3. 不同时实现路由、调度器、分布式 KV Cache、模型管理和 GPU optimizer；优先深入完成 routing + autoscaling 闭环。
4. 每个性能结论必须记录硬件、模型、软件版本、参数和 workload。
5. 先建立正确且可复现的 baseline，再进行优化。
6. 优先提交小而清晰的上游贡献，证明能够阅读并改动真实推理系统。

## 时间调整

- 每周约 5 小时：将路线延长到 8–9 个月。
- 当前精简版已假设熟悉 Prometheus 与 Kubernetes：第五阶段从 3 周压缩到 1 周，Week 5 只保留 vLLM metric contract 与实验对齐。
- 相对原 24 周版本，日历时间减少 2 周；Prometheus 基础内容再减少约 2–4 小时，总计约节省 22–28 小时（中心估算约 9%–10%）。
- 目标偏 CUDA/Kernel：增加 Triton、CUDA 和算子 profiling，弱化 AIBrix controller 开发。
- 目标偏 AI Infra/Serving：保持当前比重，重点打磨路由、扩缩容、可观测性和故障实验。
