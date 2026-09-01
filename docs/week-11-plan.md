# Week 11 Plan: 用 PyTorch Profiler 分解推理瓶颈

> 时间预算：10–12 小时
>
> 本周主线：进入推理性能专项。把 Week 10 的三个瓶颈假设转成可复现实验，用 PyTorch Profiler 分解 CPU 与 CUDA activity，识别 prefill、decode 和 mixed workload 的主要算子、同步点、内存分配与 host overhead。

## 本周目标

完成本周后，应当能够：

1. 设计低扰动、可重复的 profiler capture window。
2. 区分 CPU self time、CUDA time、kernel time、memory event 与 wall-clock latency。
3. 比较 prefill、steady decode 和 mixed workload 的 operator composition。
4. 识别显式/隐式 synchronization、频繁 allocation 与 input preparation overhead。
5. 将 profiler step 与 Week 8 scheduler step、Week 10 execution range 对齐。
6. 用 profile 证据接受或推翻至少三个瓶颈假设。

## 本周边界

- 固定 Week 6 operating point、Week 7 revision、同一 L4 和同一模型。
- Profiler run 与无 profiler baseline 分开；profile 数字不替换正式服务指标。
- 只捕获短窗口，使用 schedule 控制 wait/warmup/active。
- 本周停留在 framework/operator 层；系统级 timeline 留到 Week 12，单 kernel 深挖留到 Week 13。
- 不为了 profile 同时启用完整 scheduler/KV debug trace。
- Prometheus/Grafana 只复用作低频外部对照，不学习其基础。

## 本周最终产出

- `configs/week11-profiler.yaml`：固定 workload 与 capture window
- `scripts/run_week11_profiler.sh`：baseline/profile 成对运行
- `src/summarize_torch_profile.py`：导出 operator、shape 和 memory 摘要
- `results/week11/profiles/`：原始 trace 与 profiler metadata
- `results/week11/tables/`：CPU/CUDA top operators 和 synchronization 表
- `reports/week11.md`：三个假设的证据、结论与下一步

## Profiling Contract

每个 profile 必须绑定：

- Git commit、vLLM revision 与完整启动参数
- GPU、driver、CUDA、PyTorch 和 model revision
- workload trace ID 与 random seed
- profiler activities、schedule、record-shapes、memory 和 stack 配置
- warmup、active steps 与导出的 trace 文件
- 相同配置下无 profiler baseline 的 TTFT、TPOT 和吞吐
- profiler overhead 比例

若 profiler overhead 明显改变 batching 或 queue state，只用它解释代码路径，不做耗时占比结论。

## 四个 Capture 场景

### Scenario A：Short prefill

- 单请求，短 prompt，1 个 output token。
- 目标：识别 input preparation、embedding、attention、MLP、logits 与 sampling。

### Scenario B：Long prefill

- 单请求，固定长 prompt，1 个 output token。
- 目标：与 A 比较 GEMM/attention 占比、shape 和显存活动。

### Scenario C：Steady decode

- 请求先完成 prefill，再 capture 连续 decode steps。
- 目标：观察小 shape kernel、launch gap、sampling 和 CPU overhead。

### Scenario D：Mixed serving

- 一个请求 steady decode 时加入 chunked long prefill。
- 目标：将 scheduler composition、operator shape 与 client TTFT/TPOT 关联。

## 假设模板

每个问题先写成可证伪形式：

```text
Hypothesis: steady decode 的主要限制是 GPU kernel launch/CPU gap，而不是单个大 kernel。
Prediction: decode steps 出现大量短 kernels，kernel 之间存在可见 gap；graph replay 后 gap 减少。
Evidence: operator table + exported trace + no-profiler latency baseline。
Decision: supported / rejected / inconclusive。
```

至少覆盖：prefill dominant work、decode overhead、mixed workload interference。

## 每日安排

### Day 1：冻结实验与 profiler schedule（约 1.5 小时）

- [ ] 从 Week 10 选择三个可证伪假设
- [ ] 固定四个 workload traces
- [ ] 记录 baseline latency 和 scheduler composition
- [ ] 设计 wait/warmup/active schedule
- [ ] 限制 trace 文件大小与 capture 次数

### Day 2：Profiler API 与最小验证（约 1.5 小时）

- [ ] 运行 CPU+CUDA activities 的最小 profile
- [ ] 验证 step 标记和 trace export
- [ ] 测量 profiler overhead
- [ ] 确认 trace 中不包含 prompt 或凭证
- [ ] 记录容易引入额外 overhead 的选项

### Day 3：Short/long prefill（约 2 个计费小时）

- [ ] 运行 Scenario A/B 的 baseline/profile pair
- [ ] 导出 top CPU/CUDA operators
- [ ] 比较 input shapes、CUDA time 和 memory events
- [ ] 找到长 prefill 增加的主要工作
- [ ] 检查是否出现意外 synchronization

### Day 4：Steady decode（约 1.5–2 个计费小时）

- [ ] 只 capture warmup 后的 decode steps
- [ ] 区分 eager 与 Week 10 已验证的 graph replay
- [ ] 统计短 kernels、CPU launch 和 sampling overhead
- [ ] 检查每步 allocation 或 copy
- [ ] 不用 operator 累计时间替代 client TPOT

### Day 5：Mixed workload（约 1.5–2 个计费小时）

- [ ] 运行固定到达序列的 Scenario D
- [ ] 将 profiler step 与 scheduler trace 对齐
- [ ] 标记 long prefill chunk 和 concurrent decode
- [ ] 比较 interference 前后的 operator/shape
- [ ] 对照无 profiler TTFT/TPOT

### Day 6：分析与反证（约 1.5 小时）

- [ ] 用脚本生成统一 operator table
- [ ] 把 CPU self、CUDA total 与 wall time 分开
- [ ] 对每个假设给出 supported/rejected/inconclusive
- [ ] 选择一个值得 Week 12 系统级验证的问题
- [ ] 删除没有回答问题的冗余 trace

### Day 7：报告与复现（约 1.5 小时）

- [ ] 在干净进程中复跑最关键 capture
- [ ] 保存 profiler config、trace 与 environment manifest
- [ ] 报告 profiler overhead 和证据限制
- [ ] 为 Week 12 写出 NVTX ranges 与 Nsight capture window
- [ ] 同步结果并停止 VM

## 本周不要做什么

- 不对整场 benchmark 全程 profile。
- 不把 profiler 中的 CUDA time 相加后直接称为请求 latency。
- 不开启所有昂贵选项后仍声称低扰动。
- 不比较不同硬件、revision 或 workload 的 operator 百分比。
- 不只贴 trace 截图而不保存原始 trace 和配置。
- 不在没有 baseline pair 时声称某项 instrumentation 没有影响。

## 完成标准

- [ ] 四个场景均有 baseline/profile pair
- [ ] Capture window 能稳定命中目标 engine steps
- [ ] Profiler overhead 已量化并写入报告
- [ ] CPU、CUDA、memory 与 wall-clock 指标没有混用
- [ ] 三个瓶颈假设均有明确 verdict
- [ ] Mixed profile 能与 scheduler composition 对齐
- [ ] 原始 trace、摘要脚本和配置可复现
- [ ] Week 12 要验证的系统级问题与 NVTX 范围已确定
- [ ] GCP VM 已停止，结果已同步并绑定 Git commit
