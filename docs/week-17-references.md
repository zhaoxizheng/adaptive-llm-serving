# Week 17 Reference Reading

第十七周新增 GAIE v1.0.0、`InferencePool` v1、reference EPP conformance 边界与 Envoy `ext_proc` 数据路径。Gateway API core v1 的资源和 HTTP routing 语义直接复用 Week 16，不重复编号。

以下官方入口由 2026-09-22 的 routing research 核对。执行时固定 GAIE v1.0.0 release manifests、CRD schema 与实际 runtime status；`InferencePool` v1 稳定不等于周边所有资源和实现均稳定。

## 新增必读：GAIE v1 与 InferencePool

1. [Gateway API Inference Extension](https://gateway-api-inference-extension.sigs.k8s.io/)
   - 建立项目目标、Gateway API 集成位置、InferencePool/EPP 角色与文档导航。
   - 先区分规范、reference implementation 和生产实现，不从项目总览推导通用托管保证。

2. [GAIE API Overview](https://gateway-api-inference-extension.sigs.k8s.io/concepts/api-overview/)
   - 阅读资源之间的关系、控制面/数据面边界和请求选择路径。
   - 将 Gateway API core v1 与 inference extension API 分层记录；不要把两个 v1 标签合并成一个成熟度结论。

3. [InferencePool API Type](https://gateway-api-inference-extension.sigs.k8s.io/api-types/inferencepool/)
   - 核对 `inference.networking.k8s.io/v1`、selector、targetPorts、endpointPickerRef 与 status。
   - 重点验证 `failureMode` 的 `FailOpen`/`FailClose` 语义及未指定时默认 `FailClose`；以固定 schema 和 runtime 故障证据为准。

4. [GAIE Specification Reference](https://gateway-api-inference-extension.sigs.k8s.io/reference/spec/)
   - 用于查字段约束、默认值、conditions 与跨资源引用，不通读自动生成的全部 API。
   - 实验记录引用固定 release 的 schema；站点 `latest` 只辅助理解。

5. [Gateway API Inference Extension v1.0.0 Release](https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/tag/v1.0.0)
   - 固定 release notes、manifests 与兼容性入口，不从 `main` 拼装安装命令。
   - 安装前审查 CRD manifests；该 release asset 不提供 reference EPP Deployment/Service/image，文档示例也不替代实际 status/runtime 验收。

## 新增必读：Conformance 与外部处理

6. [GAIE Conformance](https://gateway-api-inference-extension.sigs.k8s.io/concepts/conformance/)
   - 阅读 conformance profile/范围、测试对象与结果表达，保存实际命令、版本和通过/跳过项。
   - Reference EPP 仅用于 conformance 与学习；测试通过不证明生产吞吐、HA、安全、升级或运维成熟度。

7. [Envoy External Processing Filter](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/ext_proc_filter)
   - 阅读双向 gRPC 处理流、message timeout、failure mode、statistics 与 observability。
   - 区分 EPP 不可达、ext_proc timeout、应用无候选和 upstream failure；不要把 fail-open 当成默认安全降级。
   - Envoy `latest` 可能与 gateway data plane 的嵌入版本不同，字段与 stats 名称需在固定实现中核对。

## 复用：只查本周增量问题

| 已有来源 | 本周只查什么 |
|---|---|
| [Week 16 references](week-16-references.md) #1–3 | GatewayClass/Gateway/HTTPRoute 的 core v1 角色、status 与 matching contract |
| [Week 16 references](week-16-references.md) #4 | 非 EPP weighted backends baseline，不重复学习 traffic splitting |
| [Week 15 references](week-15-references.md) #1 | Pod readiness 与 endpoint lifecycle |
| [Week 4 references](week-04-references.md) #3/#7 | vLLM streaming 与逐 Pod metrics，用于最终 endpoint 归属 |

GAIE v1.0.0 release assets 用于固定 CRDs/schema，并不包含 reference EPP Deployment/Service/image。Reference EPP 的安装命令不在阅读清单中写死；应从与 v1.0.0 对齐的 conformance 文档/源码入口另行固定 source revision 和 image digest，再保存 rendered resources 与 runtime status。Week 18 替换成 llm-d-router 时继续复用本周 `InferencePool` contract。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 新增 1–6；复用 Week 16 #1–3 | API 稳定边界、v1.0.0 与 conformance scope |
| Day 2 | 新增 3–5 | InferencePool schema、rendered manifests 与 status |
| Day 3 | 新增 2、7 | EPP/ext_proc/dispatch 数据路径与 streaming |
| Day 4 | 新增 6；回看 3–4 | Conformance、selector/ports 与 endpoint lifecycle |
| Day 5 | 新增 3、7 | FailClose、FailOpen、默认值与 timeout 故障 |
| Day 6 | 固定实现证据；复用 Week 16 #4 | 数据路径开销和实现差异 |
| Day 7 | Contract 与实验数据 | 报告和 llm-d handoff |

## 阅读后的自测问题

1. Gateway API core v1 与 InferencePool v1 分别稳定了什么，为什么不能合并成“周边全部 stable”？
2. Selector、targetPorts 与 endpointPickerRef 分别控制哪一段关系？
3. 未指定 `failureMode` 时默认值是什么，怎样用故障实验而非 YAML 外观验证？
4. Reference EPP、gateway ext_proc filter 和最终 vLLM endpoint 各自做什么？
5. EPP 返回选择后，为什么仍需证明 gateway 实际 dispatch 到该 Pod？
6. FailOpen 成功响应能否证明 EPP 正常工作，可能绕过哪些必要行为？
7. Conformance 通过为什么不能代表生产级性能、HA 或安全？
8. 为什么生产环境应使用自有 EPP 或评估 llm-d-router，而不是直接把 reference EPP 当产品？
