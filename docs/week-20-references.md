# Week 20 Reference Reading

第二十周新增的阅读只服务于最小策略接入：Envoy 外部处理边界、Go 并发验证和有界 fuzz。AIBrix 架构、路由策略和实验方法全部复用已收录资料。

新增页面于 2026-09-19 通过公开文档只读抓取核对。Envoy `latest` 可能为开发版本，字段和统计必须与实际部署版本核对。

## 新增必读

1. [Envoy External Processing Filter](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/ext_proc_filter)
   - 只读外部处理的双向 gRPC 生命周期、Statistics 和 Access Log Fields，并按需查其配置 API。
   - 区分 policy fallback、ext_proc timeout、failure-mode 和 response streaming；不把忽略错误当作安全降级。
   - 不扩展到实现新的 external processor，仍使用 AIBrix 既有入口。

2. [Go Data Race Detector](https://go.dev/doc/articles/race_detector)
   - 只读 Usage、How To Use、Requirements 和 Runtime Overhead。
   - 用于 metric/index 并发更新与请求取消；只覆盖实际运行路径，`-race` 的性能不能与正式构建对比。

## 按需新增：输入边界测试

3. [Go Fuzzing](https://go.dev/doc/security/fuzz/)
   - 只读 Writing fuzz tests、Running fuzz tests、Failing input 和 `-fuzztime`。
   - 对纯决策/输入转换函数保持 deterministic、无网络与跨用例可变全局状态，限制运行时长并保留最小失败 corpus。
   - 若没有新增解析边界，优先补 table tests；不为完成书目而搭建 fuzz 平台。

## 复用：只查本周增量问题

| 已有来源 | 本周只查什么 |
|---|---|
| [Week 16 references](week-16-references.md) #4–5 | 固定版本 routing extension、候选集合、gate/blending 与策略优先级 |
| [Week 16 references](week-16-references.md) #6 | 已有 benchmark client 的输出契约，不更换压测工具 |
| [Week 18 references](week-18-references.md) #2 | 跨进程 cache identity 的输入约束，不重读 block 生命周期 |
| [Week 19 references](week-19-references.md) #1–2 | 冻结的失败口径、重复单位和不确定性 |

集成源码沿 Week 16 保存的 upstream permalink 查找，并把最小 patch 绑定同一 revision；不新增一个随 `main` 漂移的重复源码入口。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 复用 Week 16 #4–5；新增 1 | Extension 与 ext_proc 边界 |
| Day 2 | Week 19 policy contract | 最小集成，不新增理论阅读 |
| Day 3 | 新增 2，按需 3 | 并发与边界测试 |
| Day 4 | 新增 1 的统计/错误字段 | Streaming、取消、故障 smoke |
| Day 5–6 | 复用 Week 16 #6、Week 19 #1–2 | 固定副本 A/B 与消融 |
| Day 7 | 测试记录和实验结果 | 报告与 Week 21 handoff |

## 阅读后的自测问题

1. Score 输入失效与 ext_proc gRPC 超时为什么需要不同处理？
2. Fail-open 可能绕过哪些必要行为，为什么不应作为默认修复？
3. Race detector 通过能否证明所有并发路径都安全？
4. 为什么 fuzz target 必须限制副作用、全局状态和运行时间？
5. Streaming 取消后，哪些 gateway/upstream 资源应最终释放？
6. 怎样避免把调试构建开销或附加 load gate 的变化算作策略收益？
7. 小规模 development A/B 与最终 held-out 结论之间还缺什么？
