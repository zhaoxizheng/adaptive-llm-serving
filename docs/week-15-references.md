# Week 15 Reference Reading

第十五周不重学 Kubernetes/Prometheus，只查与真实推理服务集成有关的生命周期和测量语义。重点是 request-level routing、streaming、模型冷启动和 scaling delay。

本周新增的 Kubernetes 页面已于 2026-09-14 在线核对。HPA 使用当前 canonical 路径；配置行为仍需与实验集群版本核对。

## 必读：生命周期与 HPA 语义

1. [Kubernetes Pod Lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/)
   - 只读 Pod readiness、Container probes 和 Termination of Pods/Pod shutdown。
   - 对齐 startup/readiness、endpoint 变化、`preStop` 和 termination grace 的时序；readiness 不是性能结论。

2. [Kubernetes Horizontal Pod Autoscaling](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/)
   - 只读 algorithm、Pod readiness and autoscaling metrics、stabilization 与 configurable scaling behavior。
   - 区分采样/控制周期、CPU utilization 的 requests 分母和新 Pod 尚未 Ready 的处理。
   - HPA 可使用自定义指标；本周只评估已有 CPU-based baseline，不泛化成 HPA 全部能力。

## 复用：vLLM Serving Contract

- [Week 4 references](week-04-references.md) #3–5：online serving、CLI 和 serving benchmark；验证 streaming、timeouts 与原始结果。
- [Week 4 references](week-04-references.md) #7：逐 replica 的 queue、request、token 和 KV 指标。
- [Week 5 references](week-05-references.md)：复用已有 metric contract 和 run 对齐方法，不重做监控基础建设。
- [Week 13 references](week-13-references.md) #4：实例内 APC 的 cache 状态，不能假设两个 replicas 自动共享 KV。
- [Week 14 references](week-14-references.md) #3：区分一个跨 GPU 的 model replica 与多个独立 replicas。

## 实验前必须检查的本地证据

- 固定版本的 vLLM serve help、health endpoint 与 signal/shutdown 行为。
- Gateway 的实际 LB 配置、HTTP connection reuse、SSE buffering、retry 和 timeout 规则。
- 集群 CPU metrics 是否可用、HPA targets/status/events 是否有效。
- 每个 Pod 的 GPU allocation、model/image cache 状态、node pool 最大值和当前配额。

这些属于执行时的环境验证，不能用通用文档代替；不另行安装新 gateway 或 metrics 栈来扩大本周范围。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 1，Week 4 #3–4 | GPU Pod、startup/readiness 和 endpoint |
| Day 2 | Gateway 本地配置，Week 4 #3 | 逐请求 RR、streaming、取消 |
| Day 3–4 | Week 4 #5/#7、Week 13 #4 | 分组 workload 与逐副本 cache/queue |
| Day 5 | 2 | CPU-based HPA 与 burst 时间线 |
| Day 6 | 1 | 冷启动、drain、Pod 失败 |
| Day 7 | 回看 1–2 | baseline 报告和限制 |

## 阅读后的自测问题

1. 为什么 Service 有两个 endpoints，不代表每个 HTTP 请求轮流命中它们？
2. Ready、可路由与能产生首个 token 是同一个时刻吗？
3. 如何区分请求数、token 工作量和 GPU busy time 的均衡？
4. `preStop` 与 grace period 如何共同影响长 streaming request？
5. 为什么已经输出 token 的请求不能安全地由代理自动重试？
6. HPA desired replicas 已经增加，为什么 SLO 仍可能持续恶化？
7. 固定双副本和 1–2 副本 HPA 的对比应如何说明 GPU 成本差异？
