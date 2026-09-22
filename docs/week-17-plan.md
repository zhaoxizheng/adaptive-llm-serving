# Week 17 Plan: GAIE InferencePool v1 与 Reference EPP

> 时间预算：约 11 小时
>
> 本周主线：在 Week 16 的 Gateway API v1 baseline 上固定 GAIE v1.0.0，验证 `InferencePool` v1、reference Endpoint Picker (EPP) 与 gateway `ext_proc` 数据路径，并把 conformance/学习用途和生产选型严格分开。
>
> 前置：[Week 16 plan](week-16-plan.md) 的 Gateway/HTTPRoute contract、双副本 workload 与请求归属证据；阅读：[Week 17 references](week-17-references.md)。下列文件均为计划产出，不代表仓库中已实现、已经通过 conformance 或可用于生产。

## 本周目标

1. 固定 GAIE v1.0.0 CRD/release manifests 与 gateway implementation；另行固定兼容的 reference EPP source revision 和 image digest，保存 schema 与 rendered resources。
2. 使用 `inference.networking.k8s.io/v1` 的 `InferencePool` 描述双副本候选池，核对 selector、target ports、EPP 引用和 status。
3. 追踪 Gateway → external processing → reference EPP → selected endpoint → vLLM 的实际数据路径。
4. 验证 EPP 正常、不可达、超时及 `FailOpen`/`FailClose` 的请求语义，特别确认省略 failure mode 时默认 `FailClose`。
5. 使用官方 conformance 边界评价 API/实现互操作性，不把 reference EPP 的功能结果写成生产性能保证。
6. 冻结一个可由 Week 18 替换成 llm-d Router/EPP 的 `InferencePool` contract。

## 本周边界

- `InferencePool` 当前稳定 API 是 `inference.networking.k8s.io/v1`；这不表示 GAIE 周边所有资源、协议能力或实现都已达到相同稳定级别，必须逐项核对 v1.0.0 release schema。
- Gateway API core v1 的稳定边界与 `InferencePool` v1 的稳定边界分别陈述；GAIE 不改变 Week 16 已验证的 core resource contract。
- Reference EPP 只用于 conformance、互操作学习与数据路径验证。生产环境应实现自己的 EPP 或评估 llm-d-router；本周不把 reference EPP 做容量认证。
- 不写死 reference EPP 的安装命令。GAIE CRDs 只使用固定 v1.0.0 release manifests；reference EPP 从与该 release 对齐的 conformance 文档/源码入口另行固定 revision 和 image digest。安装前审查 rendered resources，并以实际 schema、resource status 和 runtime 证据验收。
- 固定两个同型号 L4 GPU slots、vLLM images、模型/revision、workload 与 Gateway 实现；关闭 HPA/PodAutoscaler，不研究 autoscaling 或 prefix-aware routing。
- 只使用合成流量和独立实验集群；不把 `FailOpen` 当作绕过鉴权、策略或安全检查的默认生产选择。

## 本周最终产出

- `deploy/gaie/`：固定 v1.0.0 CRD manifests、另行固定的 reference EPP image digest、Gateway/route glue、`InferencePool` 与 EPP 配置。
- `configs/week17-inferencepool.yaml`：selector/ports、failure modes、workload、timeouts 与故障矩阵。
- `docs/inferencepool-epp-contract.md`：API 稳定边界、资源关系、`ext_proc` 时序与失败语义。
- `scripts/run_week17_gaie.sh`：schema/status preflight、smoke、conformance 子集、故障注入与结果导出。
- `results/week17/`：rendered manifests、conditions、request-to-endpoint attribution、EPP/gateway stats 与 raw results。
- `reports/week17.md`：互操作结论、reference EPP 限制、失败行为和 Week 18 handoff。

## API 与 Implementation Contract

| 边界 | 必须冻结或验证的内容 |
|---|---|
| Gateway API | Week 16 的 core v1 CRDs、controller/data plane release 与已验证 HTTPRoute 行为 |
| GAIE release | `v1.0.0` tag、CRD release manifests/schema 与兼容性说明 |
| InferencePool | `apiVersion: inference.networking.k8s.io/v1`、namespace/name、selector、targetPorts、endpointPickerRef 与 status |
| Reference EPP | 与 v1.0.0 对齐且单独固定的 source revision/image digest、Service/port、启动参数、endpoint discovery、健康状态和已声明用途 |
| 外部处理 | gateway 的 ext_proc 配置、request/response phase、timeout、failure mode 与 stats |
| 后端 | 被 selector 选中的 Pod UID、readiness、model identity、ports 与 endpoint lifecycle |

`InferencePool` v1 的主要字段按固定 schema 核实：

- `selector`：确认实际选中的 Pods，防止标签过宽或漏选；保存 pool membership 快照。
- `targetPorts`：确认推理流量的目标端口与 Pod 暴露端口一致，不把 EPP 端口当作模型端口。
- `endpointPickerRef`：确认引用对象、namespace/port 约束和 reference EPP 可达性。
- `endpointPickerRef.failureMode`：分别验证 `FailOpen`、`FailClose`；省略时按 API 默认 `FailClose` 验证实际结果，不能仅从 rendered YAML 猜测。

字段存在不等于 controller 已支持相应数据面行为。每次 apply 后保存 generation、conditions、events、Gateway/Route parent status、EPP health 与实际请求证据；若 release schema 或实现与预期不符，标记 unsupported/blocked，不临时换到 moving `main`。

## EPP / ext_proc 数据路径

```text
Client → Gateway / HTTPRoute
           ↓ external-processing exchange
        reference EPP ── reads InferencePool endpoints/state
           ↓ endpoint selection response
        Gateway dispatch → selected vLLM Pod → SSE tokens → Client

InferencePool controller/status → pool membership and readiness evidence
```

- 用同一 request ID 关联 gateway access log、ext_proc/EPP decision 与最终 Pod UID；不把 EPP 的“候选/选择”日志单独当成 dispatch 成功。
- 记录 external-processing request phase、round trips、timeout/error stats 和 routing latency。正式性能 run 关闭高频 body/debug logging，且永不保存 prompt、token 内容或凭证。
- Reference EPP 只证明规范学习路径和 conformance 相关行为。它的调度质量、吞吐、HA、升级、安全和运维能力不代表生产系统。
- `FailOpen` 与 `FailClose` 必须在受控 EPP failure 下测试。报告实际 fallback/HTTP 结果与最终 backend；不将 fail-open 成功响应描述为 EPP 仍在工作。
- 已开始输出 token 的 SSE 不自动迁移 endpoint；取消应传播到 gateway/upstream。Gateway 与 EPP 的 retry/timeout 分别计数，避免隐藏重复请求。

## 最小实验与 Conformance Matrix

| Cell | InferencePool / EPP 状态 | 目的 |
|---|---|---|
| A | Week 16 HTTPRoute → Service baseline | 保留相同 gateway 的非 EPP 数据路径对照 |
| B | v1 pool + reference EPP healthy | 验证 schema、membership、endpoint selection 和 streaming |
| C | 修改 selector，受控加入/移除一个 Pod | 验证 pool membership 与实际 dispatch 收敛 |
| D | 错误 target port / 无 Ready endpoint | 区分引用、连接和无可用候选的失败 |
| E | EPP unavailable/timeout + explicit `FailClose` | 验证请求显式失败且无隐藏任意 dispatch |
| F | EPP unavailable/timeout + explicit `FailOpen` | 记录固定实现的实际 fallback、status 与 backend |
| G | 省略 failure mode | 验证默认 `FailClose`，并与 E 的 runtime 结果对齐 |

- 先运行与本次 Gateway/GAIE 组合相关的官方 conformance 范围并保存命令、版本与结果；“通过某 profile/子集”不扩写成整个生态完全 conformant。
- B 使用 uniform-short 请求做足够的 endpoint attribution，加少量 long SSE 做取消/streaming smoke；不对 reference EPP 进行最大吞吐 benchmark。
- A 与 B 的低负载延迟只用于估算新增 ext_proc/EPP 路径开销；它们不是两个等价调度算法的性能对决。
- C–G 为受控小规模故障/一致性验证，不与正式延迟样本混合。每次故障前后保存 pool status、endpoint snapshot、EPP/gateway stats 和客户端结果。
- 所有变更限定在实验资源。破坏 EPP 或端口前确认不会影响共享 gateway、其他 namespaces 或真实业务。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 阅读 API/conformance，固定 v1.0.0 manifests 与 Gateway/GAIE compatibility matrix |
| Day 2 | 1.5 h | 审查 rendered resources，部署 v1 InferencePool 与 reference EPP，保存 schema/status |
| Day 3 | 2 h | 追踪 ext_proc/EPP/dispatch，完成 request-to-endpoint 与 streaming smoke |
| Day 4 | 2 h | 运行相关 conformance 范围，验证 selector、targetPorts 与 endpoint lifecycle |
| Day 5 | 1.5 h | 验证 EPP timeout/unavailable 下 FailClose、FailOpen 与默认值 |
| Day 6 | 1.5 h | 补足证据，分析数据路径开销、状态收敛与实现差异 |
| Day 7 | 1 h | 完成 contract/报告，冻结 Week 18 InferencePool handoff；同步结果并停止计费资源 |

## 报告必须回答的问题

1. 哪些资源/字段属于 Gateway API core v1，哪些属于 `InferencePool` v1，哪些周边能力仍需单独判断成熟度？
2. Selector 实际选中了哪些 Pod，targetPorts 与最终 vLLM endpoint 如何对应？
3. Reference EPP 的选择如何被证明最终由 gateway dispatch 到同一个 Pod？
4. 显式 FailClose、显式 FailOpen 和省略 failure mode 时分别发生什么，是否符合固定 schema/实现？
5. Conformance 运行证明了哪个 profile/范围，不能证明哪些性能、HA 或生产能力？
6. ext_proc/EPP 路径给低负载请求增加多少 round trips、延迟与失败面？
7. Week 18 替换 EPP 时，哪些 InferencePool/Gateway contract 必须保持不变？

## 完成标准

- [ ] GAIE v1.0.0 CRD release/manifests/schema、Gateway compatibility，以及另行固定的 reference EPP revision/image digest 均可追溯。
- [ ] `inference.networking.k8s.io/v1` InferencePool 的 selector、targetPorts、endpointPickerRef、status 均有 runtime 证据。
- [ ] Reference EPP 到最终 Pod 的 request-level 数据路径可追踪，streaming/取消语义已验证。
- [ ] FailClose、FailOpen 和默认 FailClose 有受控故障结果，无隐藏或未解释 dispatch。
- [ ] Conformance 结果精确到运行版本与范围，未提升为生产性能或通用实现保证。
- [ ] Reference EPP 明确只用于 conformance/学习；生产候选指向自有 EPP 或 Week 18 的 llm-d-router 评估。
- [ ] Week 18 可复用的 pool、route、workload、attribution 与 failure contract 已冻结，且仍明确是计划产出直到实际验收。
- [ ] 结果绑定 Git commit；GPU、LB、磁盘与公网 IP 的残余计费已检查。
