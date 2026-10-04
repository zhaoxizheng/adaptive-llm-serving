# Week 12 实验报告：Nsight Systems、GPU Gap RCA 与 Kernel Handoff

状态：`not_executed`。此文件是报告模板；代码、测试和 patch 的存在不代表已采集 GPU 证据。

配套：[中文代码导读](../docs/week-12-code-walkthrough.md)、
[学习计划](../docs/week-12-plan.md)、[共同运行约定](../docs/week-09-12-runbook.md)。

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

场景：long_prefill | eager_decode | graph_decode | mixed。逐个填写 session，不合并失败或不同参数的 runs。

| 场景 / session | 输入与 override | 原始证据路径 / event / step | 目标是否 observed | 解释与限制 |
|---|---|---|---|---|
| 待填写 | 待填写 | 待填写 | 未运行 | 待填写 |

## 必须回答的问题

1. Long prefill 与 decode 的 CUDA API、kernel 和 copy pattern 有何区别？
2. 每段主要 idle gap 的分类和直接证据是什么？
3. Eager/graph 是否通过相同逻辑 shape 检查，并看到真实 replay？
4. Blocking copy / synchronization 影响哪个阶段？
5. Mixed decode delay 来自同一步 GPU work 变长，还是等待调度？
6. Capture overhead 是否影响结论，Week 11 的三个假设如何修正？
7. Week 13 选择哪个 kernel family，需要什么 counter 来回答哪条问题？

每条结论填写：**主张 → 固定源码位置 → 实际 event/step 或 profiler row → 反证/限制**。
未观察到机制时写 `not_observed`，不要依据配置值填入成功。

## 耗时与证据边界

| 度量 | clock / units / 区间 | 实测值 | 是否受 instrumentation 影响 |
|---|---|---|---|
| Client TTFT / TPOT / wall time | 待填写 | 待填写 | 待填写 |
| CPU / CUDA / memory / block 计数 | 待填写 | 待填写 | 待填写 |

Baseline/profile pair：待填写 `pair.json` 和 overhead ratio；单 token 请求 TPOT 为 null。
Capture schedule / actual active steps：待填写。
不要把 CPU range、CUDA API duration、kernel duration 或多次 operator 累计相加称为请求延迟。

| 假设 | Prediction | 证据与反证 | Decision |
|---|---|---|---|
| Prefill dominant work | 待填写 | 待填写 | inconclusive |
| Decode overhead | 待填写 | 待填写 | inconclusive |
| Mixed interference | 待填写 | 待填写 | inconclusive |

## 交接与完成检查

- [ ] 每个场景有完整原始证据、命令、环境与 session 身份。
- [ ] Parser 通过；缺失事件、未观察到机制与失败 runs 已说明。
- [ ] 问题均由实测证据回答，尚未验证的内容明确保留未知。
- [ ] 下一周输入与待验证问题已填写。
- [ ] 结果已同步：记录路径与时间。
- [ ] VM 已停止：记录实际命令输出或云控制台证据。
- [ ] 残留磁盘、地址等资源已检查并记录。
