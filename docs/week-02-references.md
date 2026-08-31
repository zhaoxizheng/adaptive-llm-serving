# Week 2 Reference Reading

第二周围绕四个问题阅读：指标到底如何定义、batch 输入如何构造、CUDA 显存数字如何解释、batching 为什么提高吞吐却影响延迟。

## 必读：指标与性能直觉

1. [NVIDIA: LLM Benchmarking—Fundamental Concepts](https://developer.nvidia.com/blog/llm-benchmarking-fundamental-concepts/)
   - 先建立 latency、throughput、TTFT、TPOT 的共同语言。
   - 阅读时把每个定义映射到项目的 timer 起止点。

2. [NVIDIA GenAI-Perf Metrics](https://docs.nvidia.com/nim/benchmarking/llm/latest/metrics.html)
   - 重点看 TTFT、inter-token latency、request latency 和 output token throughput。
   - 注意 serving benchmark 的并发口径与本周静态 batch 口径并不相同。

3. [DistServe: Disaggregating Prefill and Decoding for Goodput-optimized LLM Serving](https://www.usenix.org/conference/osdi24/presentation/zhong-yinmin)
   - 读摘要、Introduction 和 §2。
   - 用它理解 TTFT、TPOT 为什么对应两类不同的计算阶段和 SLO。

## 必读：Batch、Padding 与 KV Cache

4. [Hugging Face: Padding and Truncation](https://huggingface.co/docs/transformers/pad_truncation)
   - 理解 batch 内不同长度输入为什么需要 padding 和 attention mask。

5. [Hugging Face: Caching](https://huggingface.co/docs/transformers/cache_explanation)
   - 复习 KV Cache shape 和 decode 时的增长方式。
   - 特别关注 batch dimension、KV heads 和 sequence length。

6. [Hugging Face: Cache Strategies](https://huggingface.co/docs/transformers/kv_cache)
   - 比较 Dynamic、Static 和 offloaded cache 的定位。
   - 本周先保持默认 Dynamic Cache，不把 cache implementation 加入正式变量。

## 必读：CUDA 显存与计时

7. [PyTorch CUDA Semantics: Memory Management](https://docs.pytorch.org/docs/stable/notes/cuda.html#memory-management)
   - 重点理解 caching allocator、allocated memory 和 reserved memory。
   - 回答为什么 `empty_cache()` 不等于释放仍被 tensor 占用的显存。

8. [torch.cuda.max_memory_allocated](https://docs.pytorch.org/docs/stable/generated/torch.cuda.max_memory_allocated.html)
   - 用于记录 tensor 实际分配峰值。

9. [torch.cuda.max_memory_reserved](https://docs.pytorch.org/docs/stable/generated/torch.cuda.max_memory_reserved.html)
   - 与 allocated peak 并列记录，用于识别 allocator cache 和碎片。

10. [PyTorch Benchmark Recipe](https://docs.pytorch.org/tutorials/recipes/recipes/benchmark.html)
    - 理解 warmup、同步、重复运行和环境噪声。
    - 本周不必迁移到 `torch.utils.benchmark`，先用它审查当前方法。

## 选读：为什么需要更好的 Batching

11. [Orca: A Distributed Serving System for Transformer-Based Generative Models](https://www.usenix.org/conference/osdi22/presentation/yu)
    - 重点阅读 iteration-level scheduling。
    - 比较静态 batch 与请求在不同时间结束时的资源浪费。

12. [FasterTransformer: Efficient Transformer Inference on GPU](https://developer.nvidia.com/blog/accelerated-inference-for-large-transformer-models-using-nvidia-fastertransformer-and-tensorrt/)
    - 作为 GPU batching 与 kernel 优化的背景材料，不要求复现。

13. [Roofline: An Insightful Visual Performance Model](https://crd.lbl.gov/assets/pubs_presos/parlab/roofline1.pdf)
    - 选读摘要和图 1。
    - 建立 arithmetic intensity 的直觉，为解释 prefill 更偏 compute-bound、decode 更偏 memory-bound 做准备。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 1–3 | 冻结指标定义 |
| Day 2 | 4–6 | 实现 batched input 和 KV 估算 |
| Day 3 | 7–10 | 实现显存记录和严谨计时 |
| Day 6–7 | 11–13 | 解释结果并连接到动态 batching |

## 阅读后的自测问题

1. 为什么 batch=8 的 token throughput 不能除以 8 后当作每个请求的吞吐？
2. `memory_allocated`、`memory_reserved` 和 `nvidia-smi` 分别测到了什么？
3. 左 padding 与右 padding 对 decoder-only batched generation 有什么影响？
4. 为什么较大的 batch 可能提高 GPU 利用率，却恶化 latency？
5. 静态 batch 的实验结果为什么不能直接预测在线 continuous batching 的 P99？
6. GQA 模型估算 KV Cache 时为什么必须用 `num_key_value_heads`？
