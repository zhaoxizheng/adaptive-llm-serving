# Week 14 Reference Reading

第十四周围绕受控 A/B 阅读，不再扩展工具列表。重点是 chunked prefill、CUDA Graph 和 TP 分别改变什么，以及如何避免混杂因素。

以下官方页面已于 2026-09-14 在线核对；原 `serving/distributed_serving/` 入口已失效，使用当前 Parallelism and Scaling 页面。实际参数和默认值仍以固定 vLLM revision 为准。

## 必读：调度与执行优化

1. [vLLM Optimization and Tuning](https://docs.vllm.ai/en/latest/configuration/optimization/)
   - 阅读 Chunked Prefill、Performance Tuning with Chunked Prefill、Preemption 与 Parallelism Strategies。
   - 将 token budget 的吞吐收益与 short/long 请求的 TTFT、TPOT 分开解释。

2. [vLLM CUDA Graphs Design](https://docs.vllm.ai/en/latest/design/cuda_graphs/)
   - 阅读 modes、BatchDescriptor、dispatcher、capture/warmup 与 backend compatibility。
   - 注意 graph mode、compilation、padding 和 capture memory 的关联，不把 `enforce-eager` 默认当作只关闭 graph。

3. [vLLM Parallelism and Scaling](https://docs.vllm.ai/en/latest/serving/parallelism_scaling/)
   - 阅读单模型 replica 的 distributed inference strategies、single-node deployment 与通信排障。
   - 先区分模型是否放得下、增加 GPU 的吞吐收益，以及通信成本；本周不要求多节点部署。

## 需要复用的前置资料

- [Week 4 references](week-04-references.md) #4、#8：CLI 和 engine args；执行时保存固定版本 help/config。
- [Week 8 references](week-08-references.md) #1、#3：scheduler 源码和 Sarathi-Serve，用于解释 chunking，不将论文等同当前实现。
- [Week 10 references](week-10-references.md) #2、#4：Model Runner 和 PyTorch graph capture 约束。
- [Week 12 references](week-12-references.md) #1–4：launch/graph/communication 的短 timeline 证据。
- [Week 13 references](week-13-references.md) #1、#4：counter 限制和 prefix caching 对照。
- [Week 6 references](week-06-references.md)：复用已验证的参数与量化方法，不新增量化格式。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 1，Week 4 #4/#8 | 固定 baseline 和实验变量 |
| Day 2 | 1，Week 8 #1/#3 | chunk budget sweep |
| Day 3 | 2，Week 10 #2/#4 | graph-only 与执行模式组合对比 |
| Day 4–5 | 回看 1–2，Week 12 #1–4 | 组合回归与机制核对 |
| Day 6 | 3 | 双卡 TP=1/2 对照 |
| Day 7 | 回看 1–3 | 冻结下一阶段 baseline |

## 阅读后的自测问题

1. 为什么提高 token budget 可能改善 long-prefill TTFT，却损害已有 decode 请求？
2. 怎么确认请求真正使用了预期 CUDA Graph，而不只是打开了配置？
3. 为什么 graph capture 的显存开销可能间接改变 KV capacity？
4. `enforce-eager` 与只禁用 CUDA Graph 在固定版本上是否等价？
5. TP=2 优于 TP=1，是否能推出它比两个 TP=1 replicas 更划算？
6. PCIe/NVLink 与 collective communication 如何限制多卡收益？
7. 单变量优化组合后出现退化时，应从哪些证据重新归因？
