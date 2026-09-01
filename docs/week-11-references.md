# Week 11 Reference Reading

第十一周使用 PyTorch Profiler 回答具体瓶颈问题。先学习 capture 与字段语义，再对固定的 prefill、decode 和 mixed workload 做短窗口 profiling。

本周首次引入 profiling 文档；vLLM tuning、CUDA timing、指标与 workload 方法分别复用 Week 4 Reference #9、Week 1 Reference #4–6、Week 4 Reference #7 和 Week 2 Reference #1–3。

## 必读：vLLM 与 PyTorch Profiling

1. [vLLM Profiling Guide](https://docs.vllm.ai/en/latest/contributing/profiling/)
   - 了解当前 vLLM 推荐的 profiler 启动、范围控制和输出方式。
   - 与固定 revision 对照，避免照搬不匹配版本的环境变量或脚本。

2. [PyTorch Profiler API](https://docs.pytorch.org/docs/stable/profiler.html)
   - 阅读 activities、schedule、step、tensorboard trace handler 和开销选项。
   - 明确 CPU time、CUDA time、shape、memory 与 stack 各自代表什么。

3. [PyTorch Profiler Recipe](https://docs.pytorch.org/tutorials/recipes/recipes/profiler_recipe.html)
   - 从最小例子掌握 operator table 和 Chrome trace 导出。
   - 将示例改造成有 wait/warmup/active 阶段的短 capture。

4. [PyTorch `record_function`](https://docs.pytorch.org/docs/stable/generated/torch.autograd.profiler.record_function.html)
   - 给 Week 10 的 prepare/forward/sample 边界添加可识别范围。
   - Range 名称只包含阶段和 step ID，不包含请求内容。

## 选读：Trace 分析

5. [Perfetto Trace Processor Documentation](https://perfetto.dev/docs/analysis/trace-processor)
   - 当 GUI 难以稳定比较多个 trace 时，用 SQL 提取 duration 与 event 关系。
   - 只在需要批量分析时使用，不把工具学习变成本周主线。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 1–2 | capture contract 与 schedule |
| Day 2 | 3–4 | 最小 profile 与自定义 ranges |
| Day 3–5 | 反复对照 1–4 | 四个 capture 场景 |
| Day 6 | 2，按需阅读 5 | 字段解释与批量分析 |
| Day 7 | 复用 Week 2 #1–3 | 报告 latency/goodput 限制 |

## 阅读后的自测问题

1. `wait`、`warmup`、`active` 和 `repeat` 如何决定实际 capture window？
2. CPU self time、CPU total、CUDA time 与 request wall time 有什么区别？
3. `record_shapes`、`profile_memory` 和 `with_stack` 分别会引入什么开销？
4. 为什么 profiler run 不能直接替代无 profiler latency baseline？
5. 如何只捕获 steady decode 而不混入 model warmup 和 prefill？
6. 如何把 profiler step 与 scheduler step、request state 对齐？
7. 什么证据可以反驳“decode 慢是因为某一个大 kernel”这一假设？
