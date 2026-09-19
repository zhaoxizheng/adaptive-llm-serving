# Week 20 Plan: 最小路由策略接入与固定副本 A/B

> 时间预算：约 11 小时
>
> 本周主线：将 Week 19 冻结的单一策略接入固定版本 AIBrix，先验证决策、并发和 streaming 行为，再用固定双副本完成小规模 A/B；本周不接入新的扩缩容逻辑。
>
> 前置：[Week 19 plan](week-19-plan.md) 的 policy/experiment contract、冻结参数及离线用例；阅读：[Week 20 references](week-20-references.md)。下列文件是待完成产出。

## 本周目标

1. 在现有 routing extension 中实现一个窄策略，不新建 gateway 或复制完整 AIBrix。
2. 保证 Ready 候选、metric freshness、取消和错误路径符合 contract。
3. 验证 metric/index 并发更新下的 snapshot 一致性与资源释放。
4. 在相同 gateway/GPU/config 下完成 baseline、candidate 和一项消融。
5. 为 Week 21 的路由 × 扩缩容组合实验留下可重复的版本、测试和测量入口。

## 本周边界

- 只在本地开发及独立实验集群运行，固定两个同型号 L4 slots、模型、engine config 与 APC。
- Workload 的 HPA/PodAutoscaler 均不写副本数；不将 autoscaling 混入本周 A/B。
- 使用 Week 19 已确认的 AIBrix 扩展接口，source patch 绑定 upstream commit；不假设最新文档接口与安装版本相同。
- 复用既有 metric/index cache，不在每次请求上新增同步 Prometheus 查询、全量扫描或无界队列。
- 不实现通用 framework、多租户优先级、PD 或 KV transfer；不在看到结果后反复调整冻结参数。

## 本周最终产出

- `router/policy/`：可复用的决策逻辑、输入约束与测试。
- 固定 AIBrix checkout 中的最小集成 diff，以及 `docs/routing-policy-contract.md` 中的 upstream commit/patch 记录。
- `configs/week20-routing.yaml`：baseline、candidate、消融、timeout/freshness 和实验配置。
- `scripts/run_week20_routing.sh`：构建版本验证、smoke、A/B 与回切 baseline。
- `results/week20/`：逐请求决策/结果、gateway overhead、错误和重复实验数据。
- `reports/week20.md`：测试覆盖、固定副本证据、负收益和 Week 21 handoff。

## 集成 Contract

| 边界 | 必须满足的行为 |
|---|---|
| 候选池 | 只选择模型匹配、Ready 且未 draining 的 Pod；最终 dispatch 前遵循框架的状态校验 |
| 快照 | Pod UID、metric/index timestamp 和一次决策的输入一致，避免读到半更新状态 |
| 异常输入 | NaN/Inf、缺失/过期值和未知 cache confidence 按 Week 19 contract 处理 |
| Fallback | 只回到同一合法候选池内的已验证 baseline；无候选明确失败 |
| Streaming | 一次 SSE 固定 upstream，不把 response buffering、重试或取消语义意外改变 |
| 超时 | 路由/外部处理有界；服务错误不应绕过鉴权或改路由到任意后端 |
| 可观测性 | 短 trace 保存 request ID、选择原因和输入时间，不保存 prompt/凭证 |

Envoy external-processing 失败不等于应用 score 缺失。不要为了保持 HTTP 200 开启 fail-open 来绕过必要的路由或安全检查；分别测试 policy fallback 与 ext_proc timeout，并保留失败计数。

## 测试分层

### 1. 本地确定性测试

- 复用 Week 19 离线用例，验证 Ready 过滤、stale metric、空候选、tie、cache unknown 和 Pod replacement。
- 校验 score 单位、极端长度、NaN/Inf 和权重边界；token 长度由可信 tokenizer/已有解析结果提供，不信任客户端自报值。
- Go table tests 覆盖不变量；有界 fuzz 只作用于本地输入转换/决策函数，不对远端服务发送异常流量。
- 对 metric refresh、cache removal、request cancellation 与并发选择运行 race detector；未被测试的路径不能声称无竞争。

### 2. Gateway 集成 smoke

- 在无 GPU stub 环境验证 request metadata、route attribution 和错误传播，但不产生性能结论。
- 在真实双 GPU 环境验证非 streaming/streaming、长请求取消、单 Pod 下线和所有候选不可用。
- 确认取消后 active request/goroutine 释放；已输出 token 后不自动重试，不能用隐藏 retry 改善成功率。
- Candidate timeout 或 ext_proc 错误可观察，回切 baseline 后重复同一 smoke。

### 3. 固定副本 A/B

| 配置 | 目的 |
|---|---|
| 冻结的既有 load/prefix baseline | 同一 gateway 的参考路径 |
| 最小 candidate | 验证 Week 19 主假设 |
| Candidate 去掉唯一新增项 | 对应主假设的一项消融，不扩展参数 sweep |

使用 Week 19 固定的 hot-prefix/mixed 主 workload 和 low-sharing 负向对照；每个正式 cell 至少三个独立重复，按计划交错顺序。保持相同 cache 初始化、offered load、GPU 数、请求长度、gate/blending 与 engine config。

- 本周只用 development/evaluation 中预先指定的小矩阵，完整 held-out campaign 留给 Week 21–22。
- 报告分组 TTFT、TPOT、SLO attainment、goodput、错误率、gateway routing latency/CPU 和 allocated/billed GPU-hours。
- `-race`、fuzz、debug logging 与 profiler runs 不用于正式性能比较；性能二进制记录相同构建配置和独立 image digest。
- No improvement 也是有效结论；若修 bug 或改参数，生成新版本并重新跑对应 baseline，不拼接不同版本结果。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 核对固定版本 extension 与数据接口，确定最小集成 diff |
| Day 2 | 2 h | 接入 policy 与既有 snapshot，补齐候选/失败行为 |
| Day 3 | 1.5 h | Table tests、race 和有界 fuzz，修复确定性失败 |
| Day 4 | 1.5 h | Stub/gateway 和真实 GPU streaming/取消/下线 smoke |
| Day 5 | 2 h | 冻结构建，运行 baseline/candidate/消融小矩阵 |
| Day 6 | 1.5 h | 完成重复与负向对照，检查 gateway overhead 和分组结果 |
| Day 7 | 1 h | 整理 patch、报告及 Week 21 handoff；同步结果并停止计费资源 |

## 报告必须回答的问题

1. 集成改了哪些最小接口，哪些现有 AIBrix 行为保持不变？
2. Ready、freshness、并发 snapshot 与 fallback 的不变量是否通过测试？
3. Streaming、取消、超时和 ext_proc 故障是否与 baseline 一致或有明确差异？
4. 候选的收益能否被一项消融解释，low-sharing 是否退化？
5. 策略计算和 metric/index 访问给 gateway 增加多少开销？
6. 哪些结果仍需完整 held-out、更多重复或扩缩容联合实验才能成立？

## 完成标准

- [ ] 最小集成 diff 可绑定 upstream commit 与构建 image，未复制整套平台。
- [ ] 关键 table/race/fuzz 测试通过；运行范围和未覆盖路径明确。
- [ ] 真实 GPU smoke 覆盖生成、streaming、取消、Pod 下线和失败路径。
- [ ] Baseline/candidate/消融保持固定副本与同一实验 contract。
- [ ] 正式性能结果未混入 race/profiler 开销，原始失败和重复均保留。
- [ ] 固定副本结论与最终项目结论分开，无收益或 blocker 如实记录。
- [ ] 回切 baseline 已验证，Week 21 的路由与 scaling 版本均已冻结。
- [ ] 所有结果同步并绑定 Git commit，GPU/LB/磁盘计费收尾完成。
