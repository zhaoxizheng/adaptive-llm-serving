# Week 3 Reference Reading

第三周围绕四个问题阅读：请求怎样进入和离开队列、什么时候 flush batch、负载接近 capacity 时为什么尾延迟急剧上升、request-level batching 与 continuous batching 有何本质区别。

以下链接已于 2026-08-31 使用浏览器在线核对。

## 必读：队列和异步实现

1. [Python `asyncio` Queues](https://docs.python.org/3/library/asyncio-queue.html)
   - 重点看 `Queue(maxsize)`、`put()`、`get()`、`task_done()` 和 shutdown 语义。
   - 注意 asyncio queue 本身没有 timeout 参数；超时需要由 `asyncio.wait_for()` 或 `asyncio.timeout()` 表达。

2. [NVIDIA Triton: Batchers](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/batcher.html)
   - 重点读 dynamic batcher 的 preferred batch size、maximum queue delay 和 queue policy。
   - 将 Triton 的配置概念映射到本周 `max_batch_size`、`max_wait_ms` 和 admission policy。

3. [Ray Serve: Dynamic Request Batching](https://docs.ray.io/en/latest/serve/advanced-guides/dyn-req-batch.html)
   - 观察 batch decorator、最大 batch size 和 batch wait timeout 如何分离。
   - 重点理解“等待更多请求”是显式 latency-throughput trade-off。

## 必读：LLM Scheduling 与尾延迟

4. [Orca: A Distributed Serving System for Transformer-Based Generative Models](https://www.usenix.org/conference/osdi22/presentation/yu)
   - 阅读 Abstract、Introduction 和 iteration-level scheduling。
   - 回答为什么一个请求完成后，传统 request-level batch 不能立即用新请求填充空位。

5. [The Tail at Scale](https://research.google/pubs/the-tail-at-scale/)
   - 重点建立 tail latency 直觉：平均值正常并不代表用户体验稳定。
   - 本周把这种直觉落实到 P95/P99、queue depth 和 rejection rate。

## 衔接 Week 4

6. [vLLM Documentation](https://docs.vllm.ai/en/latest/)
   - 浏览首页的 Serving、Benchmarking 和 Design 信息结构。
   - 不要开始逐文件读 scheduler 源码；Week 4 先把 vLLM 当作服务使用。

SLO/goodput 定义复用 Week 2 Reference #3；`bench serve` 在 Week 4 首次实际使用时阅读，不提前重复列出。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 1–3 | 队列、flush 和 overload contract |
| Day 2 | 4–5 | continuous batching 与尾延迟直觉 |
| Day 3–5 | 复用 Week 2 #3 | SLO 指标和 workload 设计 |
| Day 7 | 6 | 为 vLLM Week 4 建立文档地图 |

## 阅读后的自测问题

1. `max_batch_size=8` 时，为什么不能总是等待 batch 装满？
2. fixed window 与“从最老请求开始计时”的 size-or-time 策略有什么不同？
3. 当 arrival rate 长期超过 service rate 时，为什么任何 finite wait window 都不能修复系统？
4. 为什么必须同时报告 rejection rate 和成功请求的 P99？
5. request-level dynamic batching 在请求 output length 不同时浪费了什么？
6. 为什么 deterministic arrival trace 比每次在线随机生成到达时间更适合策略 A/B？
7. vLLM 的 continuous batching 解决了本周 scheduler 的哪一个核心限制？
