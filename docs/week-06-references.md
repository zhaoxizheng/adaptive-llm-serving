# Week 6 Reference Reading

第六周围绕三个问题阅读：vLLM 参数实际限制什么、量化减少了哪部分内存和计算、如何在性能收益之外保留正确性与可部署性证据。

以下链接已于 2026-08-31 使用浏览器在线核对。量化支持依赖 GPU capability、CUDA、vLLM 版本和 checkpoint 格式，执行时以固定版本的 support matrix 与启动日志为准。

## 必读：vLLM 参数与调优

1. [vLLM Quantization](https://docs.vllm.ai/en/latest/features/quantization/)
   - 从 support matrix 选择与 NVIDIA L4 和当前版本兼容的方案。
   - 区分预量化 checkpoint、runtime format 与 compute capability。

Engine Arguments、Optimization and Tuning、`bench serve` 分别复用 Week 4 Reference #8、#9 和 #5。所有 variant 使用相同请求与 arrival seed。

## 必读：量化方法

2. [AWQ: Activation-aware Weight Quantization for LLM Compression and Acceleration](https://arxiv.org/abs/2306.00978)
   - 阅读 Abstract、Introduction 和方法概览。
   - 理解为什么保护少量 salient weights 可以改善低比特 weight-only quantization。

3. [GPTQ: Accurate Post-Training Quantization for Generative Pre-trained Transformers](https://arxiv.org/abs/2210.17323)
   - 阅读 Abstract、Introduction 和主要结果。
   - 关注 post-training weight quantization 的目标，不深入推导全部优化过程。

4. [SmoothQuant: Accurate and Efficient Post-Training Quantization for Large Language Models](https://arxiv.org/abs/2211.10438)
   - 理解 weight-only 与 weight-activation quantization 的区别。
   - 本周即使不运行 INT8 W8A8，也用它建立 activation outlier 直觉。

## 选读：硬件与质量验证

5. [NVIDIA L4 Tensor Core GPU](https://www.nvidia.com/en-us/data-center/l4/)
   - 核对硬件支持的数值格式和显存规格。
   - 硬件支持某格式不等于当前 vLLM kernel 与 checkpoint 组合可用。

6. [Hugging Face Transformers Quantization Overview](https://huggingface.co/docs/transformers/main/en/quantization/overview)
   - 建立 checkpoint config、backend 和量化方法之间的概念地图。
   - 不把 Transformers 的支持列表直接当作 vLLM 支持列表。

7. [EleutherAI Language Model Evaluation Harness](https://github.com/EleutherAI/lm-evaluation-harness)
    - 了解后续正式质量 benchmark 的工具入口。
    - 本周只做小型 sanity check，不临时扩展成完整 eval 项目。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 1，并复用 Week 4 #5/#8/#9 | support matrix 与实验 contract |
| Day 2 | 2–4 | 量化概念和 variant 选择 |
| Day 3–5 | 回看 1–4 | 性能与参数实验 |
| Day 6–7 | 5–7 | 边界、质量和报告限制 |

## 阅读后的自测问题

1. 权重量化、activation 量化和 KV cache dtype 分别改变什么？
2. 为什么权重显存减少不保证 TPOT 或 P99 一定改善？
3. `max-num-seqs` 与 `max-num-batched-tokens` 分别限制 scheduler 的什么资源？
4. 提高 `gpu-memory-utilization` 带来什么 capacity 收益和 OOM 风险？
5. 为什么不同参数量模型不能作为严格的量化前后 A/B？
6. 一个量化 checkpoint 通过 smoke test 后，还缺哪些证据才能进入性能结论？
7. 如果量化降低显存但 goodput 不变，它仍可能在哪些场景有价值？
