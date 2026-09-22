# Week 18 Plan: llm-d Router/EPP：Load-aware 与 Precise Prefix-aware Routing

> 时间预算：约 11 小时
>
> 本周主线：保持 Week 17 的 Gateway API 与 `InferencePool` v1 contract，替换 reference EPP 为固定版本 llm-d Router/EPP，在双副本上对比 load-aware 与 precise prefix-aware pipeline，并验证 locality、排队和状态不新鲜的边界。
>
> 前置：[Week 17 plan](week-17-plan.md) 的 InferencePool、ext_proc、failure 与 request attribution contract；阅读：[Week 18 references](week-18-references.md)。下列文件均为计划产出，不代表仓库中已实现、性能收益已成立或该方案在任意环境均可托管。

## 本周目标

1. 固定 llm-d release、chart/source commit、images 与 rendered plugin pipeline，画清 Router/EPP、Gateway、InferencePool 和 vLLM scheduler 的职责。
2. 在同一数据路径上建立 load-aware baseline，核对其实际信号、刷新周期、候选过滤和 fallback。
3. 启用固定 release 支持的 precise prefix-aware 路径，区分请求历史/索引估计与 engine 实际 KV reuse。
4. 用 shared-prefix、hot-prefix 与 low-sharing workload 找出 locality 和 load 的收益/退化边界。
5. 验证 stale/missing load、Pod replacement、cache churn 与 EPP timeout 时的正确性和恢复行为。
6. 形成 Week 19 可直接使用的反例、baseline、证据字段与窄问题。

## 本周边界

- llm-d 是面向生产的开源推理栈并处于 CNCF Sandbox；这不等于所有 Kubernetes、gateway、云厂商或托管环境都提供通用兼容、SLA 或自动运维保证。
- 不写死 precise-prefix plugin 字段名。配置入口会随 release 漂移；执行时绑定 llm-d release/chart values、source commit 与实际 rendered config，并保存 plugin pipeline 顺序。
- 固定 Week 17 的 Gateway implementation、GAIE v1.0.0 `InferencePool` contract、两个同型号 L4 GPU slots、vLLM/model/revision、APC 与 workload；关闭 HPA/PodAutoscaler。
- Reference EPP 只保留为 Week 17 conformance/learning 证据，不进入本周性能矩阵；llm-d 与 reference EPP 不是生产质量的等价 A/B。
- 不部署 distributed KV tensor transfer、PD disaggregation、新存储层或多租户优先级；本周只研究 endpoint selection。
- Preble 只用于理解 locality/load tradeoff，不复刻论文系统或声称复现论文结果。
- 仅使用合成前缀和独立实验集群，不记录 prompt/token 内容；租户/模型隔离不能依赖客户端可伪造的 header。

## 本周最终产出

- `deploy/llm-d-router/`：固定 release/chart/source commit、image digests、values 与 rendered manifests。
- `configs/week18-routing.yaml`：load-aware/precise-prefix pipeline、freshness、timeouts、warmup、prefix families 与 workload。
- `docs/llm-d-routing-contract.md`：组件边界、plugin pipeline、信号/identity、fallback 与状态生命周期。
- `scripts/run_week18_llmd_routing.sh`：preflight、smoke、双副本矩阵、故障验证与结果导出。
- `results/week18/`：request decision/Pod attribution、load/cache evidence、routing latency 与逐请求结果。
- `reports/week18.md`：适用边界、反例、版本限制、Week 19 候选问题与回切路径。

## Version、Pipeline 与 Stability Contract

| 层 | 必须冻结或验证的内容 |
|---|---|
| GAIE contract | Week 17 的 `InferencePool` v1、selector、targetPorts、pool status 与 Gateway/HTTPRoute |
| llm-d Router/EPP | release/chart、source commit、images/digests、兼容矩阵与启动健康状态 |
| Plugin pipeline | 实际启用插件及顺序、候选过滤、load score、prefix score、tie/fallback 和 timeout |
| Load input | 指标名称/单位、Pod identity、sample timestamp、refresh、stale/missing 处理 |
| Prefix input | model/revision、tokenizer/chat template、token IDs、block/key identity、Pod/process generation 与 index source |
| Evidence | request ID、candidate snapshot、score/reason、selected/actual Pod、predicted cache 与 engine reuse |

官方架构页解释组件，不替代固定 release 的运行配置。每次实验保存 chart values 和 rendered manifests；从实际日志/metrics/source permalink 确认 pipeline，而不是依据概念名称猜测插件顺序。若文档中的 precise-prefix path 在固定 release 不可用或接口不兼容，将该 cell 标为 blocked，不切到 `main` 拼装一个不可复现版本。

## Routing 与 Cache Evidence Contract

```text
Client → Gateway / HTTPRoute → ext_proc
                              ↓
                      llm-d Router/EPP plugin pipeline
                         1. legal/Ready candidates
                         2. load-aware signal
                         3. precise prefix-aware signal
                              ↓
                      selected InferencePool endpoint
                              ↓
                      vLLM queue / APC / token scheduler
```

- Router/EPP 选择 endpoint，vLLM scheduler 决定实例内下一步执行哪些 tokens；不能把两层调度合写为一个算法。
- Load-aware 的 in-flight、queue、KV usage 或其他信号按固定实现核对单位和 freshness。缺失/过期不是零；Ready 也不表示无 queue。
- Precise prefix-aware 路径的“precise”必须绑定固定实现机制。Gateway/EPP predicted prefix、index residency 与 engine actual reused tokens 分开记录，不能用 route affinity 冒充 KV 命中。
- 不自行用 prompt 字符串 hash 替代 engine/cache key。模型、revision、tokenizer、chat template、adapter、Pod UID/进程代次与授权 namespace 都属于 identity contract。
- 对 stale/unknown 状态，输出正确性必须保持；性能 fallback 只进入合法 Ready 候选池。无候选显式失败，不路由到任意或不健康 endpoint。
- 正式性能 run 不记录 prompt、token 内容或完整 cache key；只保存无敏感信息的 family ID、计数、时间戳、选择原因与聚合 metrics。

## 最小实验矩阵

所有 cells 固定双副本、APC on、Gateway/InferencePool、engine config 与 pipeline 中非实验项。先以低负载 smoke 验证配置，再在 low/near-SLO 两档负载运行正式矩阵；每个 cell 至少三个独立重复并交错顺序。

| Workload | Load-aware baseline | Precise prefix-aware | 要验证的问题 |
|---|---|---|---|
| Shared-prefix，均匀 family | 必做 | 必做 | Locality 是否减少重复 prefill 并改善 TTFT/goodput |
| Hot-prefix，偏斜 family | 必做 | 必做 | 热 cache endpoint 是否因 queue 形成反例 |
| Low-sharing，长度匹配 | 必做 | 必做 | Prefix 信号无收益时是否增加路由开销或退化 |
| Mixed short/long | 必做 | 必做 | Load 与 prefix 信号对短请求尾延迟如何权衡 |

- Cold-to-warm 与 steady-state 分开；各 cell 统一 cache 清理/预热次数、前缀分布和预热成本。
- 保存 overall、short/long、prefix-family 分组的 TTFT/TPOT、SLO attainment、goodput、error、request/token load、queue、APC reuse、gateway/EPP CPU 与 routing latency。
- 先证明 load generator、gateway 和 EPP 未饱和，再解释 GPU endpoint 差异。更高 cache hit 不是成功标准；若 queue 使 P99 或 SLO 变差，保留为核心反例。
- 若 precise path 同时改变 tokenizer、candidate gate 或 failure handling，先统一非目标配置；无法隔离时将结果命名为完整 pipeline A/B，不归因单一插件。
- Preble 的机制与论文数字只用于解释实验设计；本地结果不能标为 Preble 复现。

## 故障与状态验证

1. 暂停一个 load/cache 更新源，验证 freshness 检查、fallback、错误指标与最终 endpoint；不能把 stale value 当当前值。
2. 替换一个实验 Pod，核对 Pod UID/进程代次、旧 load/cache state 清理、新 endpoint Ready 与 cold cache。
3. 用受控合成请求造成 cache churn，验证 index/affinity 状态不会无限保留已失效 residency；若实现不暴露实际 eviction 证据，明确写成未知。
4. 短暂中断 Router/EPP，在 Week 17 已验证的 failure contract 下观察 ext_proc timeout、FailOpen/FailClose 与恢复；不把 fail-open 请求计入正常策略结果。
5. 对同一请求关联 EPP decision、gateway dispatch、engine reuse 与输出校验；路由状态错误只能影响性能的结论必须有 identity/输出证据。
6. 每个故障后回切 load-aware baseline 并重复 smoke，证明实验资源恢复；不把故障 run 混入正式性能样本。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 阅读架构，冻结 llm-d release/chart/images 与 rendered plugin pipeline |
| Day 2 | 1.5 h | 接入 Week 17 InferencePool，完成 load-aware attribution/streaming smoke |
| Day 3 | 2 h | 验证 precise-prefix identity/evidence，完成 shared-prefix 主矩阵 |
| Day 4 | 2 h | 运行 hot-prefix、low-sharing 与 mixed 对照，定位 locality/load 反例 |
| Day 5 | 1.5 h | 验证 stale/missing signal、Pod replacement、cache churn 与 EPP 故障 |
| Day 6 | 1.5 h | 补足重复和分组分析，核对 gateway/EPP 开销与 Week 19 候选问题 |
| Day 7 | 1 h | 完成 contract/报告并验证回切；同步结果并停止计费资源 |

## 报告必须回答的问题

1. 固定了哪个 llm-d release/chart/source，实际 rendered plugin pipeline 和执行顺序是什么？
2. Load-aware 使用什么即时信号、单位与 freshness；缺失或过期时怎样选合法 endpoint？
3. Precise prefix-aware 的 index/预测与 engine actual reuse 如何关联，哪些 identity 条件不可缺？
4. 在什么共享度和负载下 prefix-aware 优于 baseline，何时因热点 queue 或开销而退化？
5. Pod replacement、cache churn 和信号中断后旧状态如何失效，哪些恢复行为仍未知？
6. llm-d Router/EPP 与 vLLM scheduler 各自负责什么，新增数据路径成本是多少？
7. 哪些结论只适用于本次固定组合，为什么 CNCF Sandbox/production-oriented 不构成通用托管保证？
8. Week 19 应选择哪个可重复反例，现有策略为何尚未解决？

## 完成标准

- [ ] llm-d release/chart/source commit、images、values 与 rendered pipeline 全部可追溯。
- [ ] Load-aware 与 precise prefix-aware 在同一 Gateway/InferencePool、双副本、APC 和非实验配置下对照。
- [ ] Request decision、最终 Pod、predicted prefix 与 engine reuse 可关联，未以 affinity 冒充 cache hit。
- [ ] 四类 workload 有独立重复、cold/warm 成本、负收益和分组 SLO 证据。
- [ ] Stale/missing、Pod replacement、cache churn、EPP failure 和恢复均有结果或明确 blocker。
- [ ] 报告区分 llm-d 项目定位、本次固定部署能力和通用 managed guarantee，不越界泛化。
- [ ] Week 19 的反例、baseline、证据字段和回切路径已冻结，且所有 artifacts 仍明确为计划产出直到实际验收。
- [ ] 结果绑定 Git commit；GPU、LB、磁盘与公网 IP 的残余计费已检查。
