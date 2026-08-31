# Week 2 Plan: 建立 Batch、长度、吞吐和显存的性能直觉

> 时间预算：10–12 小时
>
> 本周主线：继续使用 Hugging Face Transformers 和 GCP L4，不引入 vLLM。把第一周的单请求实验扩展为受控的长度与静态 batch 实验，建立 latency、throughput 和 memory 之间的定量关系。

## 本周目标

完成本周后，应当能够：

1. 严格区分 preprocessing、TTFT、TPOT、ITL、E2E latency、request throughput 和 token throughput。
2. 解释 prompt length 主要影响 prefill，output length 主要累积 decode 时间。
3. 解释 batch size 为什么通常提高总吞吐，却可能增加单请求延迟和显存。
4. 根据 Qwen2 配置手算 KV Cache 理论大小，并与 CUDA 实测显存变化对照。
5. 产出可复现的三组受控 sweep 和一份有明确结论的报告。

本周不追求生产级调度器。动态 batching 只做接口设计或极小原型，不与正式性能结论混在一起。

## 本周最终产出

- `configs/week02.yaml`：三个受控实验矩阵
- `src/benchmark_batch.py`：支持 padding、attention mask、静态 batch 和断点续跑
- `src/kv_cache_estimator.py`：根据模型配置估算 KV Cache
- `src/analyze_week02.py`：生成 Week 2 图表与汇总表
- `results/week02/raw/*.csv`：逐 case 原始数据
- `results/week02/figures/*.png`：四张核心图
- `reports/week02.md`：方法、结果、限制和结论
- 至少两个不依赖 GPU 的测试：KV Cache 公式和 batch 输入形状

## 指标契约

在写 benchmark 前先固定定义，避免同一个名字在代码和报告里代表不同区间。

| 指标 | 本项目 Week 2 定义 | 是否包含 tokenizer |
|---|---|---|
| `preprocessing_ms` | tokenize、padding 和 attention mask 构造时间 | 是 |
| `h2d_ms` | 输入张量复制到 GPU 的时间 | 否 |
| `gpu_ttft_ms` | 首次 model forward 完成的 GPU wall time | 否 |
| `e2e_ttft_ms` | preprocessing + H2D + 首次 forward | 是 |
| `mean_tpot_ms` | 第一个 token 之后，各 decode step 的平均时间 | 否 |
| `p95_itl_ms` | 单次生成中 decode step interval 的 nearest-rank P95 | 否 |
| `generation_ms` | prefill + 全部 decode step | 否 |
| `e2e_latency_ms` | preprocessing + H2D + generation | 是 |
| `output_tokens_per_second` | batch 内全部输出 token ÷ generation time | 否 |
| `requests_per_second` | batch size ÷ generation time | 否 |

约定：每个请求生成固定数量 token；如果以后引入 EOS 提前停止，必须同时记录 requested 和 actual output tokens。静态 batch 中所有请求一起结束，因此这里只讨论 batch completion latency，不把它冒充在线服务的 request latency distribution。

## 实验设计

正式实验全部启用 KV Cache，固定模型、dtype、GPU、greedy decoding、seed、warmup 和重复次数。每组实验只改变一个主变量。

### Sweep A：Prompt Length

回答：prompt 变长时，TTFT、显存和 decode TPOT 如何变化？

```yaml
batch_size: 1
prompt_tokens: [32, 256, 1024, 2048]
output_tokens: 64
```

### Sweep B：Output Length

回答：output 变长时，总延迟为何近似累积，而 TTFT 基本不变？

```yaml
batch_size: 1
prompt_tokens: 256
output_tokens: [16, 64, 256]
```

### Sweep C：Static Batch Size

回答：batch 增大时，总 token throughput、单请求延迟和显存如何权衡？

```yaml
batch_size: [1, 2, 4, 8, 16]
prompt_tokens: 256
output_tokens: 64
```

默认每个 case warmup 2 次、正式重复 5 次。先跑 batch `[1, 2, 4]` 的 smoke matrix，再扩大到 8 和 16。如果 OOM，把失败记录为容量边界，不要悄悄删除失败点。

## KV Cache 估算

对普通 decoder-only Transformer，单请求 KV Cache 的近似大小为：

```text
2
× num_layers
× num_kv_heads
× head_dim
× sequence_length
× bytes_per_element
```

batch 后再乘 `batch_size`。其中：

- `2` 表示 Key 和 Value。
- GQA/MQA 必须使用 `num_key_value_heads`，不能直接使用 attention heads。
- sequence length 在 decode 中持续增长，通常取 `prompt_tokens + generated_tokens`。
- 这是 KV tensor 的理论值，不包含权重、临时 activation、CUDA context、allocator cache 和框架开销。

实测时同时记录：

- `torch.cuda.max_memory_allocated()`
- `torch.cuda.max_memory_reserved()`
- 模型加载后的 allocated/reserved baseline
- 每个 case 相对 baseline 的增量

不要用 `nvidia-smi` 的进程显存直接替代 PyTorch allocator 指标；两者口径不同。

## 每日安排

## Day 1：复盘 Week 1，冻结指标定义（约 1.5 小时，本地）

- [ ] 阅读 Week 1 报告模板和 benchmark 代码
- [ ] 把上述指标契约写入 Week 2 report
- [ ] 明确第一个 output token 是否计入 TPOT 分母
- [ ] 区分 per-request latency 和 aggregate throughput
- [ ] 画出三个 sweep 的实验表，确认每次只改变一个变量

验收：不用看代码，也能说清楚每个 timer 的起止点和单位。

## Day 2：显存模型与 batch 输入（约 1.5–2 小时，本地）

- [ ] 从 Qwen2 config 读取 layer、KV head、head dimension 和 dtype
- [ ] 实现 `kv_cache_estimator.py`
- [ ] 手算 batch=1/8、sequence=320/1088 时的 KV Cache
- [ ] 学习 padding side 和 attention mask
- [ ] 为不同文本构造相同 shape 的静态 batch
- [ ] 写 KV 公式和 batch shape 单元测试

验收：估算器结果可以由一条独立公式复核；padding token 不参与有效 attention。

## Day 3：扩展 benchmark harness（约 2 小时，本地）

- [ ] 新增 `configs/week02.yaml`
- [ ] 实现 batched prefill 和 batched greedy decode
- [ ] 分开记录 preprocessing、H2D、prefill 和 decode
- [ ] 记录 allocated/reserved baseline 和 peak
- [ ] 沿用 Week 1 的原子写盘、配置指纹和断点续跑
- [ ] 输出每个 case 的完整维度，不只输出聚合值

验收：本地测试和静态检查通过；不需要启动 GPU。

## Day 4：GCP smoke 与 Prompt Sweep（约 1–1.5 个计费小时）

启动前确认本地代码已 commit。VM 上先执行：

```bash
make check-env PYTHON=.venv/bin/python
make test PYTHON=.venv/bin/python
```

然后：

- [ ] 用极小矩阵验证 batch=1 和 batch=2 输出 shape
- [ ] 验证同一输入的 batch=1 结果和 Week 1 一致
- [ ] 完成 Prompt Length Sweep
- [ ] 检查 2048-token case 是否异常波动或 OOM
- [ ] 同步原始结果并停止 VM

## Day 5：Output 与 Batch Sweep（约 1.5–2 个计费小时）

- [ ] 完成 Output Length Sweep
- [ ] 先跑 batch `[1, 2, 4]`
- [ ] 结果合理后扩展到 batch 8 和 16
- [ ] 记录每个 batch 的 aggregate token throughput
- [ ] 记录 per-request completion latency 和显存增量
- [ ] 遇到 OOM 时保存失败配置和错误，不修改已有成功结果
- [ ] 同步结果并停止 VM

## Day 6：分析与画图（约 2 小时，本地）

至少生成：

1. `prompt-length-vs-ttft.png`
2. `output-length-vs-e2e-latency.png`
3. `batch-size-vs-token-throughput.png`
4. `batch-size-vs-latency-memory.png`

汇总默认报告 median、P95；保留所有 raw repeats。图表必须标出固定变量、GPU 型号、模型和 dtype。

分析时回答：

- prompt length 翻倍时，TTFT 是否近似线性？为什么可能不是？
- output length 增大时，TPOT 是否稳定？
- 哪个 batch size 开始出现吞吐边际收益下降？
- latency 增长来自单步计算，还是更多 decode step？
- 理论 KV Cache 与实测显存增量相差多少？差额来自哪里？

## Day 7：写报告与复盘（约 1–1.5 小时，本地）

报告结构：

1. Question
2. Environment
3. Metric Contract
4. Workloads
5. Results
6. Memory Model
7. Interpretation
8. Limitations
9. Next Steps

本周结论至少包含三个带数字的句子，例如：

> 在固定 prompt=256、output=64 时，batch 从 1 增长到 8，使 aggregate output throughput 提升 X 倍，同时 batch completion latency 增长 Y%。

## 本周时间预算

| 内容 | 时间 | 地点 |
|---|---:|---|
| 指标与阅读 | 2 小时 | Mac |
| 代码与测试 | 3–3.5 小时 | Mac |
| GCP 实验 | 2.5–3.5 小时 | L4 Spot |
| 分析与报告 | 3 小时 | Mac |
| **总计** | **约 10.5–12 小时** | **GPU 约 2.5–3.5 小时** |

## 本周不要做什么

- 不引入 vLLM、Kubernetes 或 AIBrix。
- 不同时比较多个模型或 GPU。
- 不把不同 prompt 长度混在同一个静态 batch 中做正式结论。
- 不通过重复相同 prompt 冒充真实 batch，而不在报告中说明。
- 不把 batch throughput 当成在线服务在并发请求下的 request throughput。
- 不用单次运行或平均值掩盖波动和 OOM。
- 不为追求最大 batch 让 Spot VM 长时间空转调参。

## 完成标准

- [ ] 三个 sweep 都有原始 CSV、metadata 和可重画图表
- [ ] 每个结论只比较一个主变量
- [ ] 指标定义和计时边界在代码、CSV 和报告中一致
- [ ] KV Cache 理论值与实测增量有对照表
- [ ] batch throughput 和单请求 latency 没有混淆
- [ ] OOM 或异常 case 被保留并解释
- [ ] GCP VM 已停止，结果已同步
- [ ] Git 工作区干净，报告绑定明确 commit

## 与第三周的衔接

第三周在本周静态 batch 基线之上实现一个最简 dynamic batching scheduler，并研究 arrival rate、queueing delay、batch window、吞吐和尾延迟之间的关系。只有先完成本周受控实验，第三周才能区分“batch 本身的计算收益”和“排队策略引入的等待成本”。
