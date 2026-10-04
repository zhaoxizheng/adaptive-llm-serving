# Week 11 实验报告：PyTorch Profiler 与三个瓶颈假设

状态：`not_executed`。此文件是报告模板；代码、测试和 patch 的存在不代表已采集 GPU 证据。

配套：[中文代码导读](../docs/week-11-code-walkthrough.md)、
[学习计划](../docs/week-11-plan.md)、[共同运行约定](../docs/week-09-12-runbook.md)。

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

场景：short_prefill | long_prefill | decode | mixed。逐个填写 session，不合并失败或不同参数的 runs。

| 场景 / session | 输入与 override | 原始证据路径 / event / step | 目标是否 observed | 解释与限制 |
|---|---|---|---|---|
| 待填写 | 待填写 | 待填写 | 未运行 | 待填写 |

## 必须回答的问题

1. 四个窗口分别命中了哪些 active engine steps，是否确有目标 composition？
2. CPU self time、device time、kernel union 和 client latency 是否严格区分？
3. Baseline/profile 是否同 workload、同环境，开销比例是多少？
4. 哪些选项或导出过程可能改变 queue/batching？
5. 三个假设分别被支持、推翻还是仍不确定？
6. Week 12 需要用哪个系统 timeline / NVTX 范围复核？

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
