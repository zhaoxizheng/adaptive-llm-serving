# Week 16 Plan: AIBrix 架构、Gateway 与首组路由对照

> 时间预算：约 11 小时
>
> 本周主线：在 Week 15 的双副本 vLLM baseline 上接入 AIBrix，追踪 gateway 到具体 Pod 的请求路径，并完成固定副本的第一组路由对照。
>
> 前置：[Week 15 plan](week-15-plan.md) 的部署、workload 和指标基线；阅读：[Week 16 references](week-16-references.md)。下列文件是待完成产出。

## 本周目标

1. 画清 Envoy、gateway plugins、controller、runtime 与 vLLM 的职责边界。
2. 使用固定 AIBrix release 和兼容的依赖，跑通模型发现及 streaming generation。
3. 验证实际策略配置，而不只依赖 `routing-strategy` header 名称。
4. 在同 GPU 预算下比较 random、least-request，并保留 Week 15 RR 外部基线。
5. 为 Week 17 autoscaling、Week 18 cache-aware routing 明确数据依赖，不提前实现全部功能。

## 本周边界

- 固定两个 vLLM replicas；关闭实验 workload 的 HPA/PodAutoscaler，不让副本数变化干扰路由结论。
- 复用模型、dtype、engine args 和合成 workload，不同时升级后端模型或 GPU。
- 暂不启用 distributed KV transfer、PD disaggregation、GPU optimizer、LoRA 或自定义路由。
- 本周只梳理 autoscaler 与 KV event 契约；实际优化分别留到 Week 17–18。
- AIBrix 需要 Envoy Gateway；是否需要其他组件按所选 release 和工作负载核对，不默认安装整套可选组件。

## 本周最终产出

- `deploy/aibrix/`：固定 release、image digests、安装依赖和模型 discovery 配置。
- `configs/week16-routing.yaml`：策略、global/model/request override、load gates 与 workload。
- `docs/aibrix-request-path.md`：组件边界、请求时序图与固定源码 permalink。
- `scripts/run_week16_routing.sh`：smoke、固定副本策略 A/B 和结果导出。
- `results/week16/`：request-to-Pod attribution、逐副本指标、raw benchmark 与配置快照。
- `reports/week16.md`：首组路由对比、兼容性问题和 Week 17–18 handoff。

## Day 1 必须冻结的 Compatibility Matrix

| 层 | 记录项 |
|---|---|
| 集群 | Kubernetes、GPU device plugin、driver、GPU 型号和网络入口 |
| vLLM | image digest、commit/version、模型/revision、served model name、CLI/config |
| AIBrix | release/commit、CRDs、gateway/controller/runtime images |
| 依赖 | Envoy Gateway 与 Gateway API 版本、所需存储/缓存组件 |
| 发现契约 | model labels、Service、HTTPRoute、model name 和 port 的对应关系 |
| 指标契约 | 来源、字段、单位、Pod identity、刷新周期和 stale/missing 处理 |

官方 `latest` 只用于导航，示例版本不自动等于本实验兼容版本。若所选 release 要求更换 vLLM，先在新后端重新跑 Week 15 baseline；不能把后端升级收益归因给 AIBrix。

安装限定在独立实验集群；namespace 不能隔离 cluster-scoped CRDs/controllers 的影响。先核对 kube context、渲染并审查 manifests，再执行安装；不把公网未鉴权 quickstart 当作安全默认值。

## 请求链路

```text
Client → Envoy Gateway → gateway plugin / routing decision
                           ↕ model discovery + cached pod metrics
                           ↓
                       selected vLLM Pod → streamed tokens → Client

Controller → model registration / routes / desired state
Autoscaler → replica count（本周不启用）
vLLM scheduler → 当前实例的 token budget 与 KV allocation
```

记录 request ID、selected Pod UID、策略配置、response status、TTFT/TPOT、完成/取消和重试次数。只在短窗口记录必要的 routing decision，正式性能 run 关闭高频调试日志；不记录 prompt 正文或 Authorization header。

## Routing Policy Contract

当前在线文档描述了策略外的 load-imbalance gate 与自动 capacity-aware blending；安装 release 未必相同。固定源码和运行配置后回答：

- 策略来自 request header、model config 还是全局配置，优先级是什么？
- `random`/`least-request` 是否叠加了其他 score、candidate gate 或 fallback？
- In-flight 的定义是 gateway active streams、server running requests 还是其他 metric？
- 指标多旧、缺失时怎样选 Pod，Ready 状态如何传播？
- 用短 trace 验证选择，不把 response header 中的策略名当作纯算法证明。

仅在固定版本支持并可验证时关闭 blending 来做纯策略 A/B；否则保留完整实际配置，将结果命名为复合策略。不得将 `random` 重命名为 round-robin，也不假设 AIBrix 有与 Week 15 完全等价的 RR 实现。

## 最小实验矩阵

| 路径 | 策略 | 副本数 | 作用 |
|---|---|---:|---|
| Week 15 gateway | request-level RR | 2 | 外部系统基线；gateway 开销可能不同 |
| AIBrix gateway | random（记录附加机制） | 2 | 同一 gateway 的对照 |
| AIBrix gateway | least-request（记录附加机制） | 2 | 同一 gateway 下的策略对比 |

- 使用 uniform、long/short mixed、shared-prefix 三类固定 trace，低负载与 near-SLO 两档；burst 只作 smoke，不同时研究 autoscaling。
- 每个正式 cell 至少三个重复，策略交错运行；每次使用一致 cache 冷/暖流程。
- AIBrix 内部 A/B 才能较好隔离策略差异；对 Week 15 RR 的结果描述为整体路径变化，不把全部差异归因路由算法。
- Shared-prefix 只观察 locality 丢失，不在本周开启 prefix routing；命中率改善不是预设结论。
- 保存 overall 和 short/long 分组 TTFT、TPOT、goodput、error rate、逐副本 request/token load、queue/KV 与 gateway CPU/latency。
- 验证 streaming 不缓冲、client cancellation、unknown model 的明确错误以及一个副本退出后的新请求路由。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 阅读架构、冻结版本与依赖矩阵，核对独立实验集群 |
| Day 2 | 2 h | 安装最小必要组件、模型发现、HTTPRoute 状态与 generation/streaming smoke |
| Day 3 | 1.5 h | 请求到 Pod attribution，追踪实际策略、metrics cache 和 override 优先级 |
| Day 4 | 2 h | Uniform/mixed 的 random vs least-request 对照，监控 gateway 与后端瓶颈 |
| Day 5 | 1.5 h | Shared-prefix 对照及必要重复，保留 RR 外部基线 |
| Day 6 | 1.5 h | 取消、错误、单副本退出 smoke；梳理 autoscaling/KV event 数据契约 |
| Day 7 | 1 h | 完成请求路径图、报告和 Week 17–18 handoff；同步结果并停止计费资源 |

## Week 17–18 Handoff

| 后续周 | 本周只准备 | 不提前宣称 |
|---|---|---|
| Week 17：Inference-aware autoscaling | PodAutoscaler schema、指标来源/刷新、冷启动分解、HPA 对照与 GPU 上限 | 已改善 burst SLO 或成本 |
| Week 18：Cache-aware routing | tokenization/model identity、prefix key、KV event 的存储/移除、重启和 stale 状态处理 | KV event sync 等同跨节点 KV tensor transfer |

跨版本参数必须在固定 CLI/schema/source 中复核，不把文档里的 KV event 示例直接用于不同 vLLM 版本。

## 报告必须回答的问题

1. 哪个组件选择 Pod，哪个组件决定下一步运行哪些 tokens？
2. 实际执行的是纯 least-request 还是包含 gate/blending 的复合策略？
3. 相同 gateway、GPU、cache 状态下，策略差异对哪种 workload 有收益或退化？
4. Gateway 自身开销和 metrics staleness 是否足以影响结论？
5. 路由到了 Ready Pod，为什么仍可能 queue 或超 SLO？
6. 下一步应该优先改路由还是扩缩容，现有证据和未知项分别是什么？

## 完成标准

- [ ] 固定版本矩阵、最小安装与模型 discovery 可复现。
- [ ] Gateway → selected Pod → streamed response 可按 request ID 追踪。
- [ ] 实际 routing policy、附加机制、指标来源和失效行为已核实。
- [ ] 固定双副本 A/B 保留原始数据、样本量、失败和负收益。
- [ ] 请求路径图区分 controller、router、autoscaler 与 vLLM scheduler。
- [ ] Streaming、取消、unknown model 和副本退出有 smoke 证据。
- [ ] Week 17–18 数据依赖和未验证假设清晰，无越界开启功能。
- [ ] 结果同步、Git commit 与成本记录齐全，检查 GPU/LB/磁盘残余计费。
