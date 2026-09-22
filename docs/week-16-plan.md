# Week 16 Plan: Gateway API v1 L7 Baseline

> 时间预算：约 11 小时
>
> 本周主线：在 Week 15 的固定双副本 vLLM baseline 上，用 Gateway API core v1 建立可审计的 L7 matching、traffic splitting、状态与失败语义，为 Week 17 的 InferencePool 数据路径提供稳定对照。
>
> 前置：[Week 15 plan](week-15-plan.md) 的双副本、request-level attribution 与 streaming baseline；阅读：[Week 16 references](week-16-references.md)。下列文件均为计划产出，不代表仓库中已实现或已经验证。

## 本周目标

1. 固定 Gateway API CRD bundle、gateway controller、Kubernetes 与 vLLM 版本，记录实现实际支持的 core v1 能力。
2. 追踪 `GatewayClass` → `Gateway` → `HTTPRoute` → `Service` → vLLM Pod 的完整请求和状态链路。
3. 验证 hostname、path/header match 与 weighted `backendRefs` 的 L7 语义，而不只证明 YAML 可被 API server 接受。
4. 建立 request-to-route-to-backend attribution，验证 SSE streaming、取消、无匹配路由与无效 backend 的行为。
5. 冻结一套固定双副本 L7 baseline，供 Week 17 的 `InferencePool` 与 EPP 实验复用。

## 本周边界

- 只使用 `gateway.networking.k8s.io/v1` 的 core 资源和 `HTTPRoute` 主路径；不接入 GAIE、`InferencePool`、EPP 或 llm-d。
- Gateway API core v1 表示 API 的稳定边界，不表示每个 controller 都支持规范中的全部可选能力、filter 或一致行为；实现支持度必须由固定 release 的 conformance 声明、status 和 runtime 证据共同确认。
- 固定两个同型号 L4 GPU slots、两个独立 vLLM replicas、模型/revision、engine args 与 workload；关闭 HPA/其他 autoscaler。
- 所有 cross-namespace 引用先排除，backend 与 route 放在同一实验 namespace，避免把 `ReferenceGrant` 变量混入本周。
- 不比较 GAIE 或自定义调度算法，不实现 retry policy、鉴权平台或多租户治理；只记录 controller 的现有默认值。
- 仅在独立实验集群使用合成请求。入口默认 private/internal；不把未鉴权的模型 endpoint 暴露到公网。

## 本周最终产出

- `deploy/gateway-api/`：固定 CRD/controller release、image digests、`GatewayClass`、`Gateway`、`HTTPRoute` 与两个 backend Services。
- `configs/week16-l7-matrix.yaml`：hostname、path/header match、权重、workload、SLO 和失败用例。
- `docs/gateway-api-contract.md`：资源关系、status/condition、实现支持矩阵与请求时序图。
- `scripts/run_week16_gateway.sh`：preflight、apply/dry-run、smoke、L7 matrix 与结果导出入口。
- `results/week16/`：资源快照、conditions、request attribution、逐 backend 计数和 raw benchmark。
- `reports/week16.md`：标准语义、实现差异、性能开销、失败行为与 Week 17 handoff。

## Version 与 Capability Contract

| 层 | Day 1 必须冻结或验证的内容 |
|---|---|
| 集群 | Kubernetes 版本、context、节点/GPU、namespace 与网络入口 |
| Gateway API | CRD bundle/release、`gateway.networking.k8s.io/v1` schema 与安装来源 |
| 实现 | controller/gateway data-plane release、image digest、GatewayClass controller name 与实现声明的支持面 |
| 路由 | listeners、hostnames、`parentRefs`、matches、filters、`backendRefs`、port 与 weights |
| 后端 | Service selector/endpoints、Pod UID、served model name、readiness 与 target port |
| 测量 | request ID、matched route/rule、backend Pod、upstream attempts、status、TTFT/TPOT 与取消 |

规范页面的 `latest` 用于学习，不是实验版本。执行前固定一套相互兼容的 CRDs 与 controller release，先渲染并审查 manifests，再在独立集群应用。若 controller 只部分支持某个字段或 filter，把该 cell 标为 unsupported/blocked；不能因为资源被 API server 接受就写成已实现。

## L7 Routing Contract

```text
Client → Gateway address/listener
           ↓ hostname + HTTPRoute rule match
        weighted backendRef
           ↓
        Service endpoint → selected vLLM Pod → SSE tokens → Client

GatewayClass/controller → Gateway programming status
HTTPRoute parent status  → Accepted / reference resolution evidence
```

- `GatewayClass`、`Gateway` 和 `HTTPRoute` 的 generation、`status.conditions`、observed generation 与 route parent status 必须在发送流量前保存；只看 Pod Running 不足以证明路由生效。
- 每个请求带无敏感信息的 request ID，并从 gateway access log 或等价 telemetry 关联到 route、backend Service 与 Pod UID；不记录 prompt 正文或 Authorization header。
- 一次已建立的 SSE response 固定在一个 upstream；取消必须传播。若实现发生 retry，分别记录客户端请求数与 upstream attempts，不能用隐藏 retry 改善成功率。
- Header/path match 使用互斥的合成标记，避免一个请求同时命中多个实验 rule。无匹配 hostname/path 必须证明没有到达任一模型 Pod，并记录实现返回的实际 status。
- 权重表示相对流量意图，不承诺短窗口精确比例。预先固定样本量与容差/区间，并同时检查 request count、token load 和失败重试。
- Invalid backend、端口错误和未就绪 endpoint 要通过 resource status 与请求结果双向验证；不能把所有非 2xx 都归为同一种 gateway failure。

## 最小实验矩阵

| Cell | L7 配置 | 固定后端 | 回答的问题 |
|---|---|---|---|
| A | 直连单个 vLLM Service | 1 replica | 客户端与 engine 的非 gateway smoke 参考，不作同路径算法 A/B |
| B | 单一 `HTTPRoute`，100/0 | 2 replicas | Listener、route、backend 与 streaming 链路是否可追踪 |
| C | 同一路由，50/50 weighted backends | 2 replicas | 请求级分流与实现默认行为是否符合 contract |
| D | 同一路由，90/10 weighted backends | 2 replicas | 权重变化是否反映在足够样本的实际选择中 |
| E | 互斥 path/header matches | 2 replicas | L7 rule 优先级与归属能否由 runtime 证据确认 |
| F | 无匹配 route / invalid backend | 2 replicas | status、错误分类与 backend 零命中的失败语义 |

- B–E 使用同一 gateway、模型、replicas、连接策略和固定请求 trace；每个正式 cell 至少三个独立重复并交错执行。
- 以 uniform-short 为主矩阵，用一小组 long SSE 做 streaming/cancellation smoke；cache-aware workload 留到 Week 18。
- 在低负载先验证语义，再在低于 Week 15 饱和点的固定负载测量 gateway 增量；不在本周寻找最大容量。
- 保存 gateway CPU/memory、route latency、客户端 TTFT/TPOT、error/timeout 与逐 Pod request/token load；确认 load generator 和 gateway 未先饱和。
- 若实现无法给出 route/backend 归属，使用受控 backend identity response 或短窗口 access log 补证据，并在正式性能 run 关闭高频 debug。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 阅读 core v1 边界，冻结 CRD/controller、capability 与实验资源矩阵 |
| Day 2 | 1.5 h | 部署最小 GatewayClass/Gateway/HTTPRoute，保存 status 并完成 100/0 streaming smoke |
| Day 3 | 2 h | 建立 request-to-route/backend/Pod attribution，验证 path/header 与无匹配请求 |
| Day 4 | 2 h | 运行 50/50、90/10 traffic splitting 主矩阵与独立重复 |
| Day 5 | 1.5 h | 验证 invalid backend、未就绪 endpoint、取消和实际 retry 行为 |
| Day 6 | 1.5 h | 补足重复，分析分流区间、gateway 开销与实现支持差异 |
| Day 7 | 1 h | 完成 contract/报告，冻结 Week 17 baseline；同步结果并停止计费资源 |

## 报告必须回答的问题

1. 固定的是哪套 Gateway API CRDs 与哪种 controller/data plane，实际支持面如何证明？
2. 如何从一次请求关联到 listener、HTTPRoute rule、backend Service 和最终 Pod？
3. 50/50 与 90/10 的观察分布是否在预设判断范围内，retry 或请求长度是否扭曲结论？
4. Path/header/hostname 无匹配及 invalid backend 分别产生什么 status 和 runtime 行为？
5. Streaming、取消与 upstream retry 是否保持 Week 15 的请求语义？
6. Gateway 路径增加了多少延迟和资源开销，测量是否避开了饱和瓶颈？
7. 哪些结论属于 Gateway API core v1，哪些只属于本次固定 implementation/release？

## 完成标准

- [ ] Day 1 compatibility/capability matrix 含 CRD、controller、images、schema 与实现支持证据。
- [ ] GatewayClass/Gateway/HTTPRoute 的 generation、conditions 和 parent status 已保存并与 runtime 对齐。
- [ ] Request ID 可关联 route、backend 与 Pod，SSE streaming/取消没有被静默改写。
- [ ] 100/0、50/50、90/10 及 path/header cells 使用固定配置、足够样本和至少三个重复。
- [ ] 无匹配、invalid backend 与未就绪 endpoint 有明确且分开的失败证据。
- [ ] 报告区分规范稳定性、实现支持度与本次实验结果，不泛化到所有 Gateway API 实现。
- [ ] Week 17 可复用的 manifests、workload、attribution 和 baseline 已冻结，且仍明确是计划产出直到实际验收。
- [ ] 结果绑定 Git commit；GPU、LB、磁盘与公网 IP 的残余计费已检查。
