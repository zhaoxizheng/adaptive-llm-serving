# Week 6：量化与 Operating Point 报告

状态：**尚未运行 GPU 实验；未声明 AWQ 性能或质量收益。**
代码与命令见 [Week 6 导读](../docs/week-06-code-walkthrough.md)。

## 实验身份

- Week 5 handoff fingerprint / load points / SLO：待填写
- Git source、runtime、GPU UUID：待填写
- BF16 / AWQ model revision、tokenizer revision、compute / KV dtype：待填写
- 量化 backend、support matrix 复核、startup warnings：待填写
- 各阶段 session IDs 与失败记录：待填写

## 结果

| 阶段 | 选择与依据 | 显存 / P99 / goodput / error evidence |
|---|---|---|
| Representation | 待测 | 待测 |
| max_num_seqs | 待测 | 待测 |
| max_num_batched_tokens | 待测 | 待测 |
| gpu_memory_utilization（如需要） | 待测 / 有依据地跳过 | 待测 |
| Confirmation 与 soak | 待测 | 待测 |
| BF16 fallback | 待测 | 待测 |

分别报告权重加载显存、vLLM 预留显存和 KV usage；区分 `auto` 下的 BF16 / FP16 KV
数值格式，不把所有差异都归因为权重量化。

## 必须回答的问题

1. 量化节省的权重显存有没有变成可用 KV capacity 或 goodput 收益？
2. 哪些请求的 TTFT、TPOT 改善或退化？
3. 24 条 sanity check、人工抽查和错误检查各自发现了什么？
4. 最终参数为什么留有至少 10% 余量，soak 是否稳定？
5. 最终命令、回退命令、适用 mixture 与不适用场景是什么？
6. 单位合格请求成本和完整实验计费分别是多少，哪些费用尚未包含？

## 完成与资源

- [ ] 单变量阶段完成，有效 repeats 完整
- [ ] 输出人工抽查完成并记录依据
- [ ] operating point / fallback 可从证据重建
- [ ] VM 已停止，结果已同步，计费资源已核对
