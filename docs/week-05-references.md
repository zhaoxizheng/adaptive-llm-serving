# Week 5 Reference Reading

第五周不再学习 Prometheus/Grafana 基础，只围绕三个 vLLM-specific 问题阅读：当前版本暴露哪些指标、client 与 server 如何按 run 对齐、为什么 capacity 必须由 SLO goodput 而不是峰值 throughput 定义。

以下链接已于 2026-08-31 使用浏览器在线核对。vLLM 的 metric 名会随版本演进，执行时同时检查安装版本的 `/metrics` 输出。

## 必读：vLLM Metrics 与 Benchmark

1. [vLLM Production Metrics Example](https://docs.vllm.ai/en/latest/examples/online_serving/prometheus_grafana/)
   - 参考 Prometheus scrape 和 Grafana dashboard 的官方示例。
   - 只复用与已安装版本匹配的 metric，不盲目导入旧 dashboard。

2. [NVIDIA DCGM Exporter](https://docs.nvidia.com/datacenter/dcgm/latest/installation/install-dcgm-exporter.html)
   - 了解 GPU utilization、memory、power 等指标如何暴露给 Prometheus。
   - 单卡临时实验也可先用 timestamped `nvidia-smi`，但要保留相同数据语义。

## SLO 与 Goodput 背景

vLLM Metrics 与 `bench serve` 分别复用 Week 4 Reference #7 和 #5。Goodput 复用 Week 2 Reference #3，tail latency 复用 Week 3 Reference #5。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 1，并复用 Week 4 #5/#7 | metric inventory 与 benchmark contract |
| Day 2 | 2 | GPU 观测接入 |
| Day 3–5 | 复用 Week 2 #3、Week 3 #5 | SLO、goodput 与 capacity sweep |
| Day 6–7 | 回看 1 | 查询复核与报告 |

## 阅读后的自测问题

1. 当前 vLLM 版本中哪些 metric 对应 waiting request、queue time 和 KV cache usage？
2. Client TTFT 与 server queue time 之间还包含哪些阶段？
3. 为什么只报告成功请求的 P99 会掩盖过载？
4. Offered load、achieved throughput 和 goodput 有什么区别？
5. Queue depth 在 measurement window 后半段持续增长说明什么？
6. 为什么 Grafana 截图不足以支持可复现实验结论？
