# Week 4 Reference Reading

第四周先把 vLLM 当作可观测、可压测的服务使用。阅读顺序遵循“跑通 API → 理解 benchmark → 读取 metrics → 调少量参数”，暂不逐文件阅读内部实现。

以下链接已于 2026-08-31 使用浏览器在线核对；其中旧的 OpenAI-compatible Server 路径会重定向到当前 Online Serving 页面，下面使用重定向后的 canonical URL。

## 必读：安装、推理与 Serving

1. [vLLM Quickstart](https://docs.vllm.ai/en/latest/getting_started/quickstart/)
   - 跑通 offline inference 和 OpenAI-compatible server 的最短路径。
   - 对照当前 GPU 环境选择安装方式，不照抄与本机 CUDA 不匹配的命令。

2. [vLLM Offline Inference: Basic](https://docs.vllm.ai/en/latest/examples/offline_inference/basic/)
   - 理解 `LLM`、`SamplingParams` 和生成结果结构。
   - 用它完成 Day 2 的第一条 smoke path。

3. [vLLM Online Serving](https://docs.vllm.ai/en/latest/serving/online_serving/)
   - 重点看 OpenAI-compatible endpoint、模型名称和客户端调用。
   - 区分 chat template、Completions 和 Chat Completions 的要求。

4. [vLLM `serve` CLI](https://docs.vllm.ai/en/latest/cli/serve/)
   - 启动脚本的参数来源。
   - 执行时同时保存本机 `vllm serve --help`，防止文档与安装版本漂移。

## 必读：Benchmark 与指标

5. [vLLM `bench serve`](https://docs.vllm.ai/en/latest/cli/bench/serve/)
   - Week 4 的 online serving benchmark 主入口。
   - 重点看 dataset、request rate、burstiness、maximum concurrency、percentile 和 result saving。

6. [vLLM `bench throughput`](https://docs.vllm.ai/en/latest/cli/bench/throughput/)
   - 用于理解 offline throughput benchmark 的口径。
   - 不要把它的结果直接当作在线服务的 P99 或 capacity。

7. [vLLM Metrics](https://docs.vllm.ai/en/latest/design/metrics/)
   - 建立客户端指标和 server-side metrics 的映射。
   - 重点关注 running/waiting requests、queue time、token throughput 和 KV cache usage。

## 必读：参数与性能机制

8. [vLLM Engine Arguments](https://docs.vllm.ai/en/latest/configuration/engine_args/)
   - 查 `max-model-len`、`gpu-memory-utilization`、`max-num-seqs` 和 `max-num-batched-tokens`。
   - 每次实验只改变一个主参数，并把所有默认值固化到 metadata。

9. [vLLM Optimization and Tuning](https://docs.vllm.ai/en/latest/configuration/optimization/)
   - 用官方排障思路解释吞吐、延迟、preemption 和 KV cache capacity。
   - 本周先观察，不进入源码修改。

10. [vLLM Automatic Prefix Caching](https://docs.vllm.ai/en/latest/features/automatic_prefix_caching/)
    - 用于 Day 6 的隔离实验。
    - 重点理解它主要复用 shared prefix 的 prefill，不会让新 token decode 凭空变快。

## 背景论文与代码入口

11. [Efficient Memory Management for Large Language Model Serving with PagedAttention](https://arxiv.org/abs/2309.06180)
    - 阅读 Abstract、Introduction 和 PagedAttention 的核心图。
    - 本周目标是解释 vLLM 为何能更灵活地管理 KV cache，不要求推导 kernel。

12. [vLLM GitHub Repository](https://github.com/vllm-project/vllm)
    - 查看 release、issue 和 examples，确认版本相关行为。
    - 暂时不要从仓库目录顶端开始顺序读源码。

13. [Orca: A Distributed Serving System for Transformer-Based Generative Models](https://www.usenix.org/conference/osdi22/presentation/yu)
    - 复习 iteration-level scheduling，连接 Week 3 request-level batching 与 vLLM continuous batching。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 1、3、4 | 安装和 server contract |
| Day 2 | 2、3 | offline / streaming smoke |
| Day 3 | 5–7 | benchmark 与 metrics |
| Day 4–6 | 8–10 | 参数 sweep 和 prefix caching |
| Day 7 | 11–13 | 解释结果和 Week 3 A/B |

## 阅读后的自测问题

1. `vllm bench throughput` 与 `vllm bench serve` 分别回答什么问题？
2. closed-loop concurrency 和 open-loop request rate 为什么会产生不同的排队行为？
3. `max-num-seqs` 与 `max-num-batched-tokens` 分别约束什么？
4. 为什么提高 `gpu-memory-utilization` 可能增加 KV cache capacity，却不保证 P99 一定改善？
5. client TTFT 为什么不等于 server queue time？
6. continuous batching 相比 Week 3 request-level batching，如何处理不同时间完成的请求？
7. prefix caching 在什么 workload 中有收益，为什么对没有共享前缀的 workload 帮助有限？
8. 哪些证据足以支持“这个配置是可用 operating point”，而不只是“它 tokens/s 最高”？
