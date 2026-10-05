# Week 14 实验报告：优化与单节点 TP

状态：**not_executed**。入口：[代码导读](../docs/week-14-code-walkthrough.md)。

## 实验身份

待填：Git/vLLM/model revision、GPU UUID/driver/topology、engine args、SLO、workload ID、
repeat 顺序、运行窗口、初始 cache 状态、原始结果路径。

## 结果与混杂因素

| 实验 | 必需结果 | 当前状态 |
|---|---|---|
| 256/1024/4096 chunk budget | short/long/mixed/shared 分组 latency、goodput、失败 | pending |
| eager / graph-enabled | 实际 dispatch、编译设置、capture/padding、KV blocks、启动和显存 | pending |
| 组合回归 | 质量、OOM/preemption、全部 workload 与重复波动 | pending |
| 同机 TP=1/2 | raw latency、throughput、通信、per-GPU memory、GPU-seconds | pending/deferred |
| 单 Pod 双卡部署 | Pod/node/GPU allocation、visible GPU、actual rank mapping | pending/deferred |

## 选择与交接

待填：Pareto 对比、失败候选、优化帮助/损害的 workload、startup 与稳态成本、最终选择。
`eligible` 只检查错误率/客户端 lag，最终选择仍需质量、SLO 和显存审核。
若 eager 同时改变 compilation，明确写执行模式组合；缺 rank/双卡证据保持 deferred。

审核后再填写 `configs/serving-baseline.yaml` 的 selection_evidence、G、TP、topology、
镜像 digest 与 frozen。说明 Week 15 如何固定参数，TP=2 增卡实验与同预算实验分开。

## 验收

- [ ] 至少三个重复，原始请求、失败和 workload 分组齐全。
- [ ] profiler 只解释差异，容量结论来自普通服务 run。
- [ ] rank/GPU 证据或 deferred 状态明确。
- [ ] baseline 已审核、结果同步、完整计费资源检查完成。
