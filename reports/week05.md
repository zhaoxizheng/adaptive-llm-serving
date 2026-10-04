# Week 5：单实例容量与 SLO 报告

状态：**尚未运行 GPU 实验；本文件是待填写的报告模板。**
代码与命令见 [Week 5 导读](../docs/week-05-code-walkthrough.md)。

## 实验身份

- Git commit / source fingerprint：待填写
- Session IDs / 有效 run IDs：待填写
- L4 UUID、driver、CUDA、vLLM version：待填写
- 模型 revision、完整 server argv：待填写
- calibration evidence、固定 SLO 与 workload 分布：待填写
- measurement / warmup / drain 窗口、Prometheus target / scrape interval：待填写

## 容量结论

| Mixture | 有效 repeats / point | 最大稳定 rps | 第一个不稳定 rps | Goodput / error / P99 / queue 证据 |
|---|---|---|---|---|
| short_chat | 待测 | 待测 | 待测 | 待测 |
| long_context | 待测 | 待测 | 待测 | 待测 |
| generation | 待测 | 待测 | 待测 | 待测 |
| mixed | 待测 | 待测 | 待测 | 待测 |

解释客户端与 server token counter 差异；列出 timeout、counter reset、scrape gap、
客户端 saturation 等异常。保留 invalid run 的路径与原因，不静默删除。

## 必须回答的问题

1. 哪个 SLO 或稳定性条件首先被违反？
2. Mixed workload 如何影响 short chat 的尾延迟？
3. 客户端延迟、队列、GPU 采样是否支持同一结论？哪些不能直接归因？
4. 三次重复是否一致，容量是否只能定位在一个已测区间？
5. Week 6 的 low / boundary / overload 与 `handoff.json` 是否一致？

## 完成与资源

- [ ] 原始 client/Prometheus/GPU/log 证据已同步
- [ ] 五类图已生成并能从 raw 重建
- [ ] handoff ready，SLO 未在 sweep 中改变
- [ ] VM 已停止，磁盘和其他计费资源已核对
