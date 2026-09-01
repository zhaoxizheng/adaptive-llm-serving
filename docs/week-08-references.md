# Week 8 Reference Reading

第八周围绕 scheduler decision 阅读：哪些请求在每一步运行、token budget 如何分配、长 prefill 如何分块、KV cache 不足时为何发生 preemption 或 requeue。

以下链接已于 2026-08-31 使用浏览器在线核对。源码入口必须在执行时替换为 Week 7 固定 commit 的 permalink，因为 scheduler 数据结构和配置默认值会演进。

## 必读：vLLM Scheduler 源码与配置

1. [vLLM V1 Scheduler Source](https://github.com/vllm-project/vllm/blob/main/vllm/v1/core/sched/scheduler.py)
   - 重点追踪 request admission、running/waiting collections、token budget 和 preemption。
   - 从实际代码提取规则，不用本文档替代源码。

2. [vLLM V1 Request Source](https://github.com/vllm-project/vllm/blob/main/vllm/v1/request.py)
   - 复习 request state、computed/scheduled tokens 和 finish status。
   - 将状态变化映射到 trace event。

Engine Arguments 与 Optimization and Tuning 复用 Week 4 Reference #8 和 #9。默认值仍以固定版本的 CLI help 和 config dump 为准。

## 必读：Iteration-level 与 Chunked Prefill

3. [Sarathi-Serve: Taming Throughput-Latency Tradeoff in LLM Inference](https://arxiv.org/abs/2403.02310)
   - 阅读 chunked-prefills、stall-free scheduling 和主要实验结论。
   - 区分论文设计与当前 vLLM 固定版本的具体实现。

Iteration-level scheduling 复用 Week 3 Reference #4；PagedAttention 复用 Week 4 Reference #11。

## 选读：调度与服务目标

4. [Python Logging Cookbook](https://docs.python.org/3/howto/logging-cookbook.html)
   - 参考结构化、跨进程和低干扰 logging 的基本方法。
   - Trace 默认关闭，并避免在 scheduler hot path 格式化大对象。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 1–2，并复用 Week 4 #8/#9 | scheduler source map 与配置 |
| Day 2 | 3，并复用 Week 3 #4、Week 4 #11 | iteration scheduling 和 chunked prefill |
| Day 3 | 回看 1–2 | KV allocation 与 preemption 入口 |
| Day 4–6 | 4，反复对照 1 | instrumentation 与 scenarios |
| Day 7 | 复用 Week 2 #3 | 内部机制与 SLO 关联 |

## 阅读后的自测问题

1. 一个 running decode request 通常在一个 scheduler step 请求多少 token？
2. `max-num-seqs` 与 token budget 分别在哪一步阻止更多工作进入 batch？
3. Long prefill 为什么可能被拆分，拆分后对 TTFT 与已有 decode 有什么影响？
4. 如何从 trace 区分 token budget 不足和 KV cache allocation 不足？
5. Preempted request 的状态和已计算 token 会如何变化？
6. Finished 或 aborted request 的 KV blocks 在哪里释放？
7. 为什么 scheduler instrumentation 的吞吐不能直接与无 instrumentation baseline 比较？
