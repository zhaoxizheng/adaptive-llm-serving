# Week 1 Reference Reading

第一周的目标不是完整学习 Transformer 或 vLLM，而是建立一条可以解释、实现和测量的自回归推理链路。以下材料按学习顺序排列。

## 1. 必读：理解一次生成

1. [Hugging Face LLM Course: How do Transformers work?](https://huggingface.co/learn/llm-course/chapter1/4)
   - 先建立 tokenizer、Transformer、logits 和 token generation 的整体认识。
   - 第一周不需要推导全部注意力公式。

2. [Hugging Face Generation with LLMs](https://huggingface.co/docs/transformers/llm_tutorial)
   - 关注输入 token、生成参数和 autoregressive generation。
   - 对照项目中的 `src/generate.py` 阅读。

3. [Hugging Face Caching](https://huggingface.co/docs/transformers/cache_explanation)
   - 第一周最重要的原理材料。
   - 重点理解为什么 decode 可以复用历史 Key/Value，以及 cache 长度如何随 token 增长。

## 2. 必读：正确测量 CUDA

4. [PyTorch CUDA semantics](https://docs.pytorch.org/docs/stable/notes/cuda.html)
   - 重点阅读 asynchronous execution 和 synchronization。
   - 理解为什么直接用 CPU wall clock 计时可能得到错误结果。

5. [torch.cuda.synchronize](https://docs.pytorch.org/docs/stable/generated/torch.cuda.synchronize.html)
   - 对照项目中计时前后的同步调用。

6. [torch.cuda.Event](https://docs.pytorch.org/docs/stable/generated/torch.cuda.Event.html)
   - 当前代码使用 `synchronize + perf_counter`，后续可用 CUDA Event 做交叉验证。

7. [PyTorch inference mode](https://docs.pytorch.org/docs/stable/generated/torch.autograd.grad_mode.inference_mode.html)
   - 理解推理时为什么关闭 autograd，以及这和 `model.eval()` 的区别。

## 3. 必读：模型配置与实验对象

8. [Qwen2.5-0.5B-Instruct model card](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct)
   - 确认模型架构、上下文长度、推荐 Transformers 版本和使用方式。

9. [Qwen2 model configuration](https://huggingface.co/docs/transformers/model_doc/qwen2#transformers.Qwen2Config)
   - 查看 `num_hidden_layers`、`num_attention_heads`、`num_key_value_heads` 和 `hidden_size`。
   - 用这些参数手算一个 sequence length 下的 KV Cache 大小。

## 4. GCP 实验环境

10. [Create a G2 or G4 instance](https://cloud.google.com/compute/docs/gpus/create-gpu-vm-g-series)
    - 理解 G2/L4 VM 的创建和限制。

11. [Create and use Spot VMs](https://cloud.google.com/compute/docs/instances/create-use-spot)
    - 重点阅读抢占、停止动作和 Spot 无 SLA 的语义。

12. [Install GPU drivers](https://cloud.google.com/compute/docs/gpus/install-drivers-gpu)
    - 项目的 `bootstrap_gcp.sh` 使用这里提供的 Google 安装器。

13. [GCP GPU regions and zones](https://cloud.google.com/compute/docs/gpus/gpu-regions-zones)
    - 创建失败时先核对目标 zone 是否支持 G2，再判断是 quota 还是临时容量问题。

## 5. 选读：建立系统视角

14. [Orca: A Distributed Serving System for Transformer-Based Generative Models](https://www.usenix.org/conference/osdi22/presentation/yu)
    - 第一周只读摘要、Introduction 和 iteration-level scheduling 部分。
    - 它解释了为什么生成式模型 serving 不能简单照搬传统 request batching。

15. [Efficient Memory Management for Large Language Model Serving with PagedAttention](https://arxiv.org/abs/2309.06180)
    - 第一周只读摘要和 Introduction，暂不深入 vLLM 实现。
    - 先记住问题：传统 KV Cache 管理为什么会产生浪费和碎片。

## 推荐阅读节奏

| 时间 | 内容 | 目标 |
|---|---|---|
| Day 1 | 1–3 | 能画出 tokenizer → prefill → decode → output 的数据流 |
| Day 2 | 8–9 | 能用模型配置手算 KV Cache 大小 |
| Day 3 | 10–13 | 能安全创建、停止和恢复 GCP Spot VM |
| Day 4 | 4–7 | 能解释 CUDA 异步执行和当前计时方法 |
| Day 6–7 | 14–15 | 把第一周单请求实验放入 serving 系统背景中 |

阅读完成标准不是“看完链接”，而是可以回答：

1. prefill 和 decode 的输入形状为什么不同？
2. KV Cache 保存了什么，为什么能降低 decode 的重复计算？
3. 为什么 CUDA benchmark 需要同步和 warmup？
4. TTFT、TPOT、总生成时间和吞吐分别说明什么？
5. Spot VM 中断后，哪些状态会保留，实验如何继续？
