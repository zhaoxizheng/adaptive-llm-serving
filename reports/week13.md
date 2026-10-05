# Week 13 实验报告：Kernel 与 Prefix Caching

状态：**not_executed**。代码与 CPU 测试不代表实际 counter/APC 结果。
入口：[代码导读](../docs/week-13-code-walkthrough.md)。

## 版本与原始结果

待填：Git commit/dirty state、vLLM source、模型 revision、GPU/driver、dtype/backend、
ncu 版本、SLO、低负载/near-SLO RPS、每个 cell 的 session 与 trace fingerprint。

## Kernel 假设

| 项目 | 实际证据 |
|---|---|
| Week 12 hotspot / 时间占比 / calls / 阶段 | pending |
| kernel / shape / launch / process/rank | pending |
| sections / units / raw report | pending |
| replay passes / cache / clock / 权限 | pending |
| 支持、推翻或未决的假设 | pending |

## 无 profiler APC 对照

每个 A/B/C/D cell、负载与重复记录：请求数、失败/超时、分组 P50/P95/P99 TTFT/TPOT、
goodput、cache hit/query 定义与增量、recomputed prefill、queue/preemption、priming 成本。
附 cold→warm 的 TTFT/metric 时间序列；D 的低重合需要 token 级证据。

## 结论与 Week 14 handoff

待回答：counter 排除了什么替代解释？APC 是否仅在部分负载有收益？预热成本多久摊平？
TPOT 变化是否有独立执行证据？选哪个参数继续验证，baseline/SLO 如何冻结？

## 验收与成本

- [ ] 真实 kernel report/counter 语义已审核；权限阻塞保持未完成。
- [ ] 无 profiler A/B 同 trace，至少三个重复，失败保留在分母。
- [ ] cache 初始化、priming、输出质量、样本限制与负收益已说明。
- [ ] 结果同步、Git identity、GPU/磁盘账单与资源停止情况已记录。
