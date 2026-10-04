# Week 10 实验报告：GPU Worker、Model Runner 与执行边界

状态：`not_executed`。此文件是报告模板；代码、测试和 patch 的存在不代表已采集 GPU 证据。

配套：[中文代码导读](../docs/week-10-code-walkthrough.md)、
[学习计划](../docs/week-10-plan.md)、[共同运行约定](../docs/week-09-12-runbook.md)。

## 实验身份

| 项目 | 实测记录 |
|---|---|
| 日期 / VM / GPU UUID | 待填写 |
| Repo commit / dirty state / config fingerprint | 待填写 |
| vLLM commit / source fingerprint / import path | 待填写 |
| 模型与 tokenizer revision / 启动参数 | 待填写 |
| GPU / driver / CUDA / PyTorch / dependency freeze | 待填写 |
| Week 6 operating point 或 fallback 证据 | 待填写 |
| Workload ID / seed / session 路径 | 待填写 |

## 场景证据

场景：prefill | decode | mixed | eager | graph。逐个填写 session，不合并失败或不同参数的 runs。

| 场景 / session | 输入与 override | 原始证据路径 / event / step | 目标是否 observed | 解释与限制 |
|---|---|---|---|---|
| 待填写 | 待填写 | 待填写 | 未运行 | 待填写 |

## 必须回答的问题

1. 模型权重、memory profiling、KV 初始化、warmup/capture 的顺序是什么？
2. SchedulerOutput 如何改变 persistent batch 与 GPU 输入？
3. Prefill / decode / mixed 的 logical shape、padding 和 slots 有何差异？
4. 哪个 step 证明实际 eager / graph replay，而不仅是配置允许？
5. Sampling / output copy 在哪里发生等待，哪些时间尚不能解释为 GPU duration？
6. 列出三条可由 Week 11 profile 验证的瓶颈假设。

每条结论填写：**主张 → 固定源码位置 → 实际 event/step 或 profiler row → 反证/限制**。
未观察到机制时写 `not_observed`，不要依据配置值填入成功。

## 耗时与证据边界

| 度量 | clock / units / 区间 | 实测值 | 是否受 instrumentation 影响 |
|---|---|---|---|
| Client TTFT / TPOT / wall time | 待填写 | 待填写 | 待填写 |
| CPU / CUDA / memory / block 计数 | 待填写 | 待填写 | 待填写 |

Trace instrumentation 影响：待填写。短机制实验不更新 Week 5/6 正式容量结论。

## 交接与完成检查

- [ ] 每个场景有完整原始证据、命令、环境与 session 身份。
- [ ] Parser 通过；缺失事件、未观察到机制与失败 runs 已说明。
- [ ] 问题均由实测证据回答，尚未验证的内容明确保留未知。
- [ ] 下一周输入与待验证问题已填写。
- [ ] 结果已同步：记录路径与时间。
- [ ] VM 已停止：记录实际命令输出或云控制台证据。
- [ ] 残留磁盘、地址等资源已检查并记录。
