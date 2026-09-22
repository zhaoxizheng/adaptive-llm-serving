# Week 18 Reference Reading

第十八周新增 llm-d Router/EPP 的架构与 load-/precise-prefix-aware 路径，再用 Preble 和 vLLM prefix caching design 校准 locality/load 及 cache identity。Gateway API core v1、InferencePool v1 与 Envoy `ext_proc` 直接复用 Week 16–17，不重复编号。

以下官方/原始来源由 2026-09-22 的 routing research 核对。llm-d 配置名会随 release/chart 漂移，执行必须保存固定 release、chart values、source commit 和 rendered plugin pipeline；文档页面只提供概念入口。

## 新增必读：llm-d Router/EPP

1. [llm-d Router Architecture](https://llm-d.ai/docs/architecture/core/router)
   - 先读 Router 在 llm-d 推理栈、Gateway API/GAIE 与 backend 之间的位置。
   - 区分 production-oriented 的设计目标、CNCF Sandbox 项目状态和本地已验证能力；不能推导所有环境都有 managed SLA。

2. [llm-d Endpoint Picker (EPP)](https://llm-d.ai/docs/architecture/core/router/epp)
   - 阅读 EPP 组件、候选 endpoint、plugin pipeline、request metadata 与选择输出。
   - 重点核对 Router/EPP 与 vLLM scheduler 的职责边界；实际插件顺序以固定 release 的 rendered config/source 为准。

3. [llm-d Precise Prefix Cache Routing](https://llm-d.ai/docs/well-lit-paths/foundations/precise-prefix-cache-routing)
   - 阅读 well-lit path 的前提、部署组件、prefix/cache 信号和验证方法。
   - 不把页面中的字段名复制成长期 contract；从固定 release/chart 导航到真实 values/schema 并保存 rendered pipeline。
   - 若固定 release 无该路径或链接迁移，从该 release 的文档导航查找并记录替代 permalink，不切换 moving `main`。

4. [llm-d Router Repository](https://github.com/llm-d/llm-d-router)
   - 固定 tag/commit，定位 pipeline construction、load/prefix plugins、fallback 和 metrics 的源码 permalink。
   - README 或 `main` 仅用于导航；构建、镜像与运行证据必须绑定同一 revision。

## 新增必读：Locality、Load 与 Cache Identity

5. [Preble: Efficient Distributed Prompt Scheduling for LLM Serving](https://arxiv.org/abs/2407.00023)
   - 阅读问题定义、locality/load-balancing 取舍和评估条件，重点理解“只追求 cache hit”为何可能形成热点。
   - 固定论文版本；只用来设计反例，不复刻系统，也不将论文数字当作本地收益。

6. [vLLM Prefix Caching Design](https://docs.vllm.ai/en/latest/design/prefix_caching/)
   - 只读 block hash components、序列化一致性和 cache isolation/security。
   - APC 用户语义已在 Week 4 首次收录，block allocation/free 已在 Week 9 学过；本周新增的是跨组件 identity 与证据边界。
   - Hash/salt 不等于可信身份或授权，固定 vLLM 是否支持相关配置需以版本和 runtime 核对。

## 复用：只查本周增量问题

| 已有来源 | 本周只查什么 |
|---|---|
| [Week 17 references](week-17-references.md) #2–4 | InferencePool/EPP API contract、status 与字段约束 |
| [Week 17 references](week-17-references.md) #7 | ext_proc timeout/stats/failure 边界，不重复编号 Envoy 来源 |
| [Week 16 references](week-16-references.md) #3 | Gateway/HTTPRoute attribution 与 streaming contract |
| [Week 4 references](week-04-references.md) #7/#10 | Engine metrics 与实例内 APC，不重学基础操作 |
| [Week 9 references](week-09-references.md) #1–3 | Block lifecycle/eviction 的源码语义，用于检查实际 reuse/residency |

Cold/warm 流程直接复用 [Week 13 plan](week-13-plan.md)。Reference EPP 的定位与 conformance 证据留在 Week 17；本周只把它替换为 llm-d Router/EPP，不做不公平的生产性能对照。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 新增 1–4；复用 Week 17 #2–4 | 组件边界、版本与 rendered plugin pipeline |
| Day 2 | 新增 2、4；复用 Week 17 #7 | Load-aware 数据路径与 request attribution |
| Day 3 | 新增 3、6；复用 Week 4 #10 | Precise-prefix identity、预测与实际 reuse |
| Day 4 | 新增 5；回看 2–3 | Shared/hot/low-sharing 的 locality/load 反例 |
| Day 5 | 新增 4、6；复用 Week 9 #1–3 | Stale state、Pod replacement、churn 与恢复 |
| Day 6 | 固定源码/metrics 与实验数据 | 分组结果、开销和 Week 19 候选问题 |
| Day 7 | Contract 与实验结果 | 报告、回切和 handoff |

## 阅读后的自测问题

1. llm-d Router/EPP 与 vLLM scheduler 分别决定什么？
2. 为什么 llm-d production-oriented 且属于 CNCF Sandbox，仍不能代表任意环境都有通用 managed guarantee？
3. 如何从固定 chart values 和 rendered config 证明实际 plugin pipeline，而不是依赖可能漂移的字段名？
4. Load metric 缺失或过期时，为什么不能按零负载处理？
5. Precise prefix prediction、index residency 和 engine actual reused tokens 有什么区别？
6. 文本相同为什么仍可能因 model/tokenizer/template/adapter identity 不同而无法安全共享 cache？
7. 为什么 cache hit 更高可能同时让热点 Pod 的 P99 TTFT 更差？
8. Pod replacement 或 cache churn 后，哪些旧状态必须失效，怎样证明恢复？
9. Preble 论文机制与本地 llm-d pipeline A/B 之间为什么不能画等号？
