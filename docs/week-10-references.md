# Week 10 Reference Reading

第十周从 scheduler output 继续追踪到 GPU execution。阅读重点是 Worker 和 Model Runner 的真实调用边界、输入准备、CUDA Graph dispatch、model forward 与 sampling。

源码链接使用 `main` 作为入口发现；正式 source map 必须替换为固定 commit permalink。vLLM 架构、engine args 和 tuning 分别复用 Week 7 Reference #1、Week 4 Reference #8–9。

## 必读：Worker 与 Model Runner

1. [vLLM V1 GPU Worker Source](https://github.com/vllm-project/vllm/blob/main/vllm/v1/worker/gpu_worker.py)
   - 追踪 device/model 初始化、memory profiling、KV cache 初始化和 model execution。
   - 区分 Worker 的进程职责与 Model Runner 的每步执行职责。

2. [vLLM V1 GPU Model Runner Source](https://github.com/vllm-project/vllm/blob/main/vllm/v1/worker/gpu_model_runner.py)
   - 重点阅读 scheduler output 更新、input preparation、execute model 和 sampling。
   - 只沿当前模型和 attention backend 实际命中的分支。

## 必读：CUDA Graph

3. [vLLM CUDA Graphs Design](https://docs.vllm.ai/en/latest/design/cuda_graphs/)
   - 理解 graph capture modes、capture sizes 与运行时 dispatch。
   - 对照固定 revision，确认文档描述与当前配置是否一致。

4. [PyTorch `torch.cuda.graph`](https://docs.pytorch.org/docs/stable/generated/torch.cuda.graph.html)
   - 理解 capture、replay、静态地址和 stream 约束。
   - 用于解释机制，不用简化示例替代 vLLM 实现。

## 需要复用的前置资料

- API、Engine Core 与进程边界：复用 Week 7 Reference #1、#5–6。
- Scheduler output 与 request state：复用 Week 8 Reference #1–2。
- KV block table：复用 Week 9 Reference #1–3。
- CUDA timing 与 synchronization：复用 Week 1 Reference #4–6。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 1，复用 Week 7 #1/#6 | worker initialization |
| Day 2–4 | 2，复用 Week 8 #1–2、Week 9 #1–3 | input、forward、sampling |
| Day 5 | 回看 1–2 | execution trace 与 shape ledger |
| Day 6 | 3–4 | graph capture 与 replay |
| Day 7 | 固定 commit permalinks | 端到端 source map |

## 阅读后的自测问题

1. GPU Worker 与 GPU Model Runner 分别持有哪些长期状态？
2. Memory profiling 如何影响可创建的 KV cache blocks 数量？
3. Scheduler output 如何更新 persistent input batch？
4. Prefill、decode 和 mixed step 的 tensor shape 与 attention metadata 有何差异？
5. 当前版本如何决定使用 eager execution 还是某个 captured graph？
6. Model forward、logits processing 与 sampling 分别在哪里执行？
7. 哪些 runtime 证据能证明 CUDA Graph 确实被 replay？
