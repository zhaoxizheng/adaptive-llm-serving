# Week 15 Plan: 多副本 vLLM 集成与 Round-robin 基线

> 时间预算：约 11 小时
>
> 本周主线：复用已掌握的 Kubernetes 和 Prometheus，在真实双 GPU 环境建立多副本 baseline，验证路由、请求排空和冷启动的实际行为，为后续 Gateway API/GAIE 对照提供公平证据。
>
> 前置：[Week 14 plan](week-14-plan.md) 的固定 serving baseline；阅读：[Week 15 references](week-15-references.md)。下列文件是待完成产出。

## 本周目标

1. 在同型号 GPU 上运行两个独立 vLLM replicas，每个副本独占一张 GPU。
2. 验证真正的 request-level round-robin，而不是把 Service 的连接分发当成 RR。
3. 在 uniform、mixed、shared-prefix、burst 四类流量下建立逐副本证据。
4. 测量 Pod cold start、Ready、route admission 和 first successful token 的时间差。
5. 验证正常请求排空与异常断流，并保存明确的失败语义。

## 本周边界

- 不重新学习 Deployment、Service、Probe、Prometheus 或 HPA 基础。
- 不安装 Gateway API/GAIE 或其他推理网关扩展，不实现 cache-aware/SLO-aware router。
- 不将两个进程挤在同一张 GPU 上冒充双副本性能实验。
- 测试只在独立实验集群/namespace；不复用工作环境的配置、凭证或真实流量。
- CPU-only 集群可做 manifest/control-plane smoke，但不能替代 GPU 性能与容量验收。

## 本周最终产出

- `deploy/vllm/`：固定 image digest、模型 revision、GPU resources、probes 和 shutdown 配置。
- `deploy/gateway/`：最小 request-level RR gateway 配置与 upstream 身份记录。
- `deploy/autoscaling/`：已存在 HPA 模板的本实验配置，不引入新 autoscaler。
- `configs/week15-multireplica.yaml`：workload、SLO、cache 状态和副本矩阵。
- `scripts/run_week15_baseline.sh`：部署验收、baseline 与 lifecycle 实验入口。
- `results/week15/`：逐请求/逐副本指标、HPA events、cold-start 和 drain timeline。
- `reports/week15.md`：多副本 baseline、观察到的限制和后续 Gateway API/GAIE handoff。

## 资源与安全前置

- 使用 GKE 或已有独立 GPU Kubernetes，至少有两个可同时分配的同型号 GPU slots；GPU 不足则本周性能部分 blocked。
- 固定 GPU 型号、驱动、vLLM image、模型、dtype、engine args、CPU/memory requests 与 limits。
- 性能实验优先按需节点，避免 Spot 抢占与调度策略混杂；如用 Spot，抢占 run 单独标记。
- 默认 private/internal endpoint。外部访问必须有 TLS、鉴权和源地址限制，不暴露未鉴权模型或管理接口。
- 先检查 quota、node pool 上限、模型下载与磁盘预算；不自动创建无上限 GPU 资源。
- 模型缓存可复用，但 cold-start 报告明确区分 image/model cached 与 uncached。

## 路由与 Streaming Contract

```text
Load generator → request-level RR gateway
                    ├── vLLM replica A → GPU 0
                    └── vLLM replica B → GPU 1
```

- RR 的选择单位是一次 HTTP inference request；一次 SSE stream 从头到尾固定在同一 upstream。
- 普通 Kubernetes Service 不保证逐请求 RR；HTTP keep-alive/HTTP2 连接复用可能让流量长期落在同一后端。
- 用 request ID 关联 gateway upstream、Pod UID 与客户端结果，实际证明分发序列，不凭配置名称判断。
- 两个健康副本的每轮 RR admission count 差值应不超过 1；多 gateway worker 时按各自序列验证，再报告总分布。
- Readiness 通过不等于首条模型请求已可用，增加受控 warmup 和真实 generation smoke。
- 不在已输出 token 后自动重试；取消要传递给 upstream，重试次数、断流和重复生成均可观测。
- 记录 routing、connect、queue、TTFT、TPOT 的测量边界。客户端重试不隐藏原始失败。

## 最小实验矩阵

| 场景 | 固定双副本 RR | HPA 基线 | 重点证据 |
|---|---|---|---|
| Uniform short | 必做 | 不必单独做 | 流量分配、客户端瓶颈、服务开销 |
| Long/short mixed | 必做 | 可选 | 每副本 queue、短请求 P99、token 工作量不均 |
| Shared prefixes | 必做 | 不做 | 每副本 cache hit/query 增量、cache locality 与 TTFT |
| Burst | 必做 | 必做 | desired/current/Ready replicas、冷启动与 SLO attainment |

- 双副本 RR 使用完全相同的输入 trace 和每实例配置，与单副本对比时明确 GPU 预算翻倍。
- HPA 使用已有 CPU-based 模板，设置合理 CPU requests、目标值、stabilization、min=1/max=2；预先保留两张 GPU 容量，先隔离 Pod scaling 而不是 node provisioning。
- Burst 额外保留固定单副本/双副本两端基线。HPA 不是同资源预算对比，必须报告时间变化的 GPU allocation 和 GPU-hours。
- 若 CPU 未触发 HPA，报告“该指标/目标在此 workload 未触发”，不预设 HPA 必然失败；HPA 也支持 custom metrics，不把 CPU 指标局限等同整个机制局限。
- 每个正式 cell 至少三个重复，保存请求数、cache 初始状态、失败/超时和逐请求 latency。
- 先证明 load generator 与 gateway 未饱和，再解释 GPU 副本差异；不能用 request 数均衡推导 token 工作量均衡。

## Lifecycle 实验

1. 无业务流量时记录 schedule → image pull → model load → readiness → route admission → first token。
2. 有长 streaming request 时执行实验 Deployment 的正常滚动更新，观察旧 Pod 是否停止接新请求并排空已有请求。
3. 核对 endpoint removal、gateway 更新延迟、`preStop` 与 termination grace 的实际顺序，不假设所有组件瞬时同步。
4. 只在实验范围内触发一个 Pod 失败，记录中断请求、恢复时间与后续请求可用性；不要求中断 SSE 可以透明恢复。
5. 记录 empty/warm model cache 的启动差异；每次滚动更新前确认临时额外 GPU 需求和预算。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | GPU quota/预算检查、固定 manifests、单副本部署 smoke |
| Day 2 | 1.5 h | 双副本与 RR 验证，检查 connection reuse、streaming 和 request ID |
| Day 3 | 2 h | Uniform/mixed workload，无瓶颈客户端和逐副本指标对齐 |
| Day 4 | 1.5 h | Shared-prefix workload，分析 cache 状态而非只看请求数 |
| Day 5 | 2 h | 固定副本与 HPA burst 对照，记录副本时间线和 GPU 成本 |
| Day 6 | 1.5 h | Cold start、正常 drain 与单 Pod 失败；运行必要重复 |
| Day 7 | 1 h | 完成 baseline 报告、冻结后续 Gateway API/GAIE 对照输入、同步结果并清理计费资源 |

## 报告必须回答的问题

1. 如何证明这是 request-level RR，而不是连接级均衡？
2. 请求数均衡时，token load、queue 和 cache locality 是否仍不均衡？
3. 多副本增益是多少，代价是多少 GPU-hours，何时不再线性扩展？
4. Burst 中观测到压力、desired replicas 增加、Pod Ready 和首个 token 之间各有多久？
5. CPU 指标对本 workload 有多强解释力，缺失了什么推理压力信息？
6. 正常下线是否排空了已有请求，强制失败丢失了哪些请求？
7. 哪个 workload 最适合后续 Gateway API/GAIE 路由对照，哪些结论仍是待验证假设？

## 完成标准

- [ ] 双副本真实运行在两个独占 GPU slots，版本和资源配置一致。
- [ ] RR 分发、SSE 不缓冲、取消和失败语义有请求级证据。
- [ ] 四类 workload 有原始数据，失败、样本量和成本未遗漏。
- [ ] HPA burst 有 desired/current/Ready 时间线，和固定单/双副本基线可对照。
- [ ] Cold start、drain 与 Pod 失败都有实际观察，不仅是 YAML 配置。
- [ ] 结果不预设 RR/HPA 必然更差，限制和反例保留。
- [ ] 后续 Gateway API/GAIE 对照使用的后端配置、workload 和指标定义已固定。
- [ ] 结果同步后检查 GPU 节点、磁盘、LB 和公网 IP 的残余计费。
