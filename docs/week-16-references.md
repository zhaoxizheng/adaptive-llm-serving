# Week 16 Reference Reading

第十六周只深入 AIBrix 架构、安装契约、gateway 和 benchmark。Autoscaling 与 KV events 本周只建立边界，分别在 Week 17–18 继续。

以下官方页面已于 2026-09-14 在线核对。文档中的 `latest`、示例镜像与源码入口可能不同步，实际运行必须保存 release/commit、image digests 和兼容性矩阵。

## 必读：架构与安装

1. [AIBrix Architecture](https://aibrix.readthedocs.io/latest/designs/architecture.html)
   - 区分 control plane 与 data plane，理解 router、runtime、controller 和 GPU optimizer 的职责。
   - 阅读不等于全部启用；本周只安装双副本 gateway 实验所需组件。

2. [AIBrix Installation](https://aibrix.readthedocs.io/latest/getting_started/installation/installation.html)
   - 阅读 stable installation、组件依赖和 individual components。
   - 核对 Envoy Gateway 与 CRDs；KubeRay 是否需要取决于选定的部署模式和 release。

3. [AIBrix Quickstart](https://aibrix.readthedocs.io/latest/getting_started/quickstart.html)
   - 只读 base-model deployment 与 gateway invocation，暂跳过 PD disaggregation。
   - 检查 served model name、Service、model labels、port 和 HTTPRoute 的发现契约。
   - 不直接使用公网未鉴权 endpoint；先落实实验网络和鉴权边界。

## 必读：路由与 Benchmark

4. [AIBrix Router Design](https://aibrix.readthedocs.io/latest/designs/aibrix-router.html)
   - 阅读 Envoy external processing、gateway plugin、pod metrics cache 与 request sequence。
   - 围绕一次请求追踪，不按目录通读整个项目；源码记录需固定 permalink。

5. [AIBrix Gateway Routing](https://aibrix.readthedocs.io/latest/features/gateway-plugins.html)
   - 阅读 routing strategies、header/global 配置、debugging 和 metrics access。
   - 当前页面包含 load gate 和 auto-blended capacity awareness：必须确认固定 release 的实际行为，不能只凭策略名做纯算法对比。
   - 对 streaming、fallback 与取消的行为保留 runtime 证据。

6. [AIBrix Benchmark and Workload Generator](https://aibrix.readthedocs.io/latest/features/benchmark-and-generator.html)
   - 区分 dataset generation、workload shaping、benchmark client 和 analysis。
   - 优先复用既有 harness；若采用新 client，先验证 TTFT/TPOT、arrival trace、错误与 goodput 口径一致。
   - 不使用一次短 quickstart 的 P99 作为容量结论。

## 浏览：后续两周的边界

7. [AIBrix Autoscaling](https://aibrix.readthedocs.io/latest/features/autoscaling/autoscaling.html)
   - 本周只读 PodAutoscaler、metric source、scaling target 与算法选择概览。
   - 记录与 Kubernetes HPA 的区别及 Week 17 所需输入，不开启副本变化。

8. [AIBrix KV Cache Events Synchronization](https://aibrix.readthedocs.io/latest/features/kv-event-sync.html)
   - 本周只读架构、事件流、tokenization 前提与 cache index 生命周期。
   - KV event sync 同步 cache 状态信息，不等同于传输 KV tensors。
   - 配置样例必须与固定 vLLM CLI、AIBrix build 和 event schema 核对，不能跨版本直接照搬。

## 需要复用的前置资料

- [Week 15 references](week-15-references.md)：request-level RR、cold start、drain 与 HPA baseline。
- [Week 4 references](week-04-references.md) #10：实例内 prefix caching；cold/warm 实验复用 [Week 13 plan](week-13-plan.md)。
- [Week 4 references](week-04-references.md) #7：vLLM metric 语义，不把 gateway in-flight 与 server running 混成一个计数。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 1–2 | 职责边界、版本与依赖矩阵 |
| Day 2 | 2–3 | 最小安装、discovery、streaming smoke |
| Day 3 | 4–5 | 请求路径、实际策略与 metric cache |
| Day 4–5 | 5–6 | 同 gateway 的策略 A/B |
| Day 6 | 7–8，仅概览 | Week 17–18 数据契约 |
| Day 7 | 回看 4–6 | 请求路径图、报告与未知项 |

## 阅读后的自测问题

1. 为什么 AIBrix router 和 vLLM scheduler 不是同一层决策？
2. 哪些依赖属于必要路径，哪些只为可选功能服务？
3. Request header、model profile 和 global routing config 的优先级如何验证？
4. `least-request` 是否只按请求数选 Pod，哪些 gate/blending 会改变结果？
5. 为什么 AIBrix random 与 Week 15 RR 的差异不能全部归因于算法？
6. Metrics cache 的时效性如何影响 routing decision？
7. Benchmark client 更换后，需要核对哪些口径才能复用旧 baseline？
8. Cache state event、prefix-aware routing 与 KV tensor transfer 的边界分别是什么？
