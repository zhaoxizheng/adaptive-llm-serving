# Week 8：Scheduler Decision 报告

状态：**已提供 trace patch 和 parser；尚未运行真实 scheduler 场景。**
代码与图表解释见 [Week 8 导读](../docs/week-08-code-walkthrough.md)。

## 实验身份

- 固定 vLLM commit、实际 import path、两份 patch：待填写运行证据
- Git commit、L4、模型、Week 6 operating point：待填写
- 每场景 session、server argv、request shapes 与到达时刻：待填写

## 四场景证据

| 场景 | 预期 | Trace 观察 | Client 影响 | 结论 / 未观察原因 |
|---|---|---|---|---|
| baseline | 最小 prefill/decode/free 链路 | 待测 | 待测 | 待测 |
| mixed | 长短请求 admission 与交错 | 待测 | 待测 | 待测 |
| token_pressure | prompt 被分到多个 step | 待测 | 待测 | 待测 |
| kv_pressure | allocation failure / preempt / requeue | 待测 | 待测 | 待测 |

## 必须回答的问题

1. Sequence limit、token budget、KV capacity 分别在哪个条件阻止 admission？
2. Chunked prefill 如何影响 long request TTFT 与已有 decode 的 TPOT？
3. 哪些证据能区分 token pressure 与 KV allocation failure？
4. Preemption 后 computed tokens、waiting state 与后续调度如何变化？
5. 所有请求是否最终完成或明确 abort/error，blocks 在哪里释放？
6. 内部 queue 如何与 Week 5 外部指标对应，哪些时间不能精确归因？

## 完成与交接

- [ ] Parser 的预算、状态、sequence、terminal invariant 全部通过
- [ ] CSV、五类图与原始 JSONL 对应
- [ ] Chunked prefill 与 KV pressure 已观察，或清楚记录缺失原因
- [ ] Week 9 block manager / prefix reuse / eviction 问题已列出
- [ ] trace 已同步，VM 与计费资源已核对
