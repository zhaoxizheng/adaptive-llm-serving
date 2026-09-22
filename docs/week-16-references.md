# Week 16 Reference Reading

第十六周只新增 Gateway API core v1 的 L7 标准语义：资源关系、HTTP matching 与 weighted traffic splitting。GAIE、`InferencePool`、EPP 和 llm-d 留到 Week 17–18，不用后续扩展反推本周 baseline。

以下官方入口由 2026-09-22 的 routing research 核对。页面会随站点更新，执行版本必须绑定具体 Gateway API CRD bundle 与 controller release；core v1 稳定不等于每个实现支持全部可选能力。

## 新增必读：Gateway API v1 Baseline

1. [Gateway API Introduction](https://gateway-api.sigs.k8s.io/docs/introduction/)
   - 先建立 `GatewayClass`、`Gateway`、Route 与 backend 的角色边界，并区分 API 规范和 controller 实现。
   - 重点理解面向角色的资源模型；本周不扩展到 mesh、GAIE 或自定义 policy。

2. [Gateway API Guides](https://gateway-api.sigs.k8s.io/guides/)
   - 用作 core concepts 与实现差异的导航页，不按目录通读全部 guide。
   - 记录本实验实际使用的标准/支持级别；页面存在某个 guide 不表示固定 controller 已实现该功能。

3. [HTTP Routing](https://gateway-api.sigs.k8s.io/guides/http-routing/)
   - 聚焦 listener/hostname、`parentRefs`、path/header matches、`backendRefs` 与 route status。
   - 将 manifest intent、resource conditions 和实际 backend attribution 三者对齐；只被 API server 接受不是运行成功。

4. [HTTP Traffic Splitting](https://gateway-api.sigs.k8s.io/guides/traffic-splitting/)
   - 阅读 weighted `backendRefs` 的相对权重语义及 progressive delivery 示例。
   - 权重不是短窗口的逐请求确定序列；预先定义样本量与判断规则，并检查 retry 对 backend attempts 的影响。

## 复用：只查本周增量问题

| 已有来源 | 本周只查什么 |
|---|---|
| [Week 15 references](week-15-references.md) #1 | Pod readiness、endpoint 变化与 shutdown；不重学 Pod lifecycle |
| [Week 4 references](week-04-references.md) #3–5 | vLLM online serving、SSE client 与 benchmark 输出 contract |
| [Week 4 references](week-04-references.md) #7 | 逐 Pod request/queue/token 指标，不把 gateway 计数与 engine 计数混用 |
| [Week 15 plan](week-15-plan.md) | 固定双副本、request ID、连接复用和取消 baseline |

Controller 的支持声明、rendered manifests、status、access logs 与 metrics 属于执行时本地证据，不新增一个随 `latest` 漂移的实现书目。若规范和固定实现行为不同，以保存的版本与 runtime 证据描述本次结果，同时单列规范预期。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 新增 1–2 | 资源角色、core v1 与 implementation capability matrix |
| Day 2 | 新增 3 | 最小 Gateway/HTTPRoute 与 status 验收 |
| Day 3 | 新增 3；复用 Week 15 plan | Matching、request attribution 与 streaming |
| Day 4 | 新增 4 | 50/50、90/10 weighted splitting |
| Day 5 | 新增 3–4；复用 Week 15 #1 | 无匹配、invalid backend、endpoint 与取消 |
| Day 6 | 回看 2–4；固定实现证据 | 支持差异、分流区间与 gateway 开销 |
| Day 7 | Contract 与实验数据 | 报告和 Week 17 handoff |

## 阅读后的自测问题

1. `GatewayClass`、`Gateway`、`HTTPRoute` 和 `Service` 分别由谁配置、由谁实现？
2. 为什么 core v1 资源被 API server 接受，仍不能证明 controller 支持所有字段或 filter？
3. 哪些 resource/parent conditions 能说明 route 已被接受并解析引用，为什么还需要 runtime 请求证据？
4. Hostname、path 与 header match 如何设计成互斥用例？
5. 为什么 weighted backends 不应在很小样本中被要求精确等于 50/50？
6. Retry 为什么会让客户端请求分布与 upstream attempt 分布不同？
7. 一条 SSE stream 的 backend 归属与普通短请求分流有什么不同？
8. 哪些结果可以归因于 Gateway API contract，哪些只能归因于固定 controller release？
