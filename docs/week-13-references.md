# Week 13 Reference Reading

第十三周把阅读分成 kernel 诊断与 prefix caching 两条线：前者解释执行机制，后者通过服务 A/B 验证用户可见收益。

以下官方页面已于 2026-09-14 在线核对。执行时固定 NVIDIA 工具、driver 和 vLLM 版本；`latest` 是发现入口，不是实验版本锁。

## 必读：Nsight Compute

1. [NVIDIA Nsight Compute Profiling Guide](https://docs.nvidia.com/nsight-compute/ProfilingGuide/index.html)
   - 重点阅读 Metric Collection、Replay、Overhead、Reproducibility、Cache Control 和 Roofline Charts。
   - 回答一次报告经历多少 replay passes，以及 cache/clock policy 如何影响 counter。

2. [NVIDIA Nsight Compute CLI](https://docs.nvidia.com/nsight-compute/NsightComputeCli/index.html)
   - 阅读 kernel/NVTX filtering、multi-process support、launch selection、sections、export/import 与 CSV。
   - 先定位 vLLM 实际执行 kernel 的 worker，再收集少量 launches；不使用全量 counters 起步。

3. [NVIDIA Performance Counter Permission Troubleshooting](https://developer.nvidia.com/nvidia-development-tools-solutions-err_nvgpuctrperm-permission-issue-performance-counters)
   - 在租用长时 GPU 前检查 profiler 权限，理解 `ERR_NVGPUCTRPERM`。
   - 权限处理依赖 driver 和云平台；只在获授权环境中按管理员规范处理，不照抄全局放权步骤。

## 必读：Prefix Caching

4. [vLLM Automatic Prefix Caching](https://docs.vllm.ai/en/latest/features/automatic_prefix_caching/)
   - 复习 shared prefix 对 prefill 的复用，以及对新 token decode 的限制。
   - 本周从“开关可用”推进到 cache cold/warm、低重合对照和 near-SLO 的受控实验。

5. [vLLM Optimization and Tuning](https://docs.vllm.ai/en/latest/configuration/optimization/)
   - 重点复核 preemption、KV capacity 与 batch/token budget 的关系。
   - 不把提升 hit rate 等同于改善所有 workload 的 P99。

## 需要复用的前置资料

- [Week 12 references](week-12-references.md) #1–3：kernel 选择的 timeline 与 NVTX 证据。
- [Week 9 references](week-09-references.md) #1–3：block lookup、reference、free 和 eviction 源码。
- [Week 4 references](week-04-references.md) #7、#11：cache metrics 口径和 PagedAttention 背景。
- [Week 2 references](week-02-references.md)：latency、goodput 与 workload 的测量方法。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 1–3，Week 12 #1–3 | 权限、kernel target、采集扰动 |
| Day 2–3 | 1–2 | 最小 counter report 与瓶颈判断 |
| Day 4 | 4，Week 9 #1–3 | prefix families 与 cache 状态 |
| Day 5–6 | 5，Week 4 #7 | near-SLO 对照与指标单位 |
| Day 7 | 回看 1、4–5 | 写出证据限制和下一步优化 |

## 阅读后的自测问题

1. Nsight Systems 与 Nsight Compute 分别回答什么问题？
2. Replay 和 cache control 为什么可能改变真实服务中的缓存行为？
3. 为什么 occupancy 高低不能单独决定 kernel 的效率？
4. 如何证明被采集的 kernel/shape 与 Week 12 hotspot 一致？
5. Cold model、cold prefix cache 和 cold GPU memory cache 为什么不同？
6. Prefix hit rate 的分子分母是 token、block 还是 request，为什么必须确认？
7. Warm prefix 的收益为什么必须与预热成本、cache churn 和无 profiler goodput 一起报告？
