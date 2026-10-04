# Week 2–4 代码 Review 与修复记录

Review 日期：2026-10-04。修复前源码基线：`accc4f07d6c137fe0935857f175250a8fb64d5a0`。
本次发现的六项代码问题均已修复，并更新三份中文 walkthrough。

覆盖三个配置、runner、HF batch backend、调度器、校准、SSE/trace client、vLLM 生命周期、
benchmark wrapper、分析和 verifier。未执行真实 GPU benchmark 或更改云资源；涉及 vLLM
默认行为的结论核对了上游 `v0.10.2` 源码。

## 修复结果

| 编号 | 原优先级 | 修复前的问题 | 当前行为 |
|---|---|---|---|
| [R1](#r1) | P1 | prefix caching=false 未显式关闭 | 严格 boolean 配置，显式生成开启/关闭参数 |
| [R6](#r6) | P1 | smoke 进入要求 overload 数据的分析 | smoke 生成 summary/analysis；primary 仍要求四张图 |
| [R2](#r2) | P2 | Week 3 与 replay 吞吐分母不同 | 统一声明窗口，另存 drain 指标，恢复和 verifier 重算 |
| [R3](#r3) | P2 | 0.75 负载曲线混入参数扫描 | 主曲线和跨周 loader 先筛选默认 batch 参数 |
| [R4](#r4) | P2 | operating point 只要求一次 repeat 达标 | 完整 repeat 集全部达标才入选 |
| [R5](#r5) | P2 | 输出根未覆盖 HF 前置产物 | 前置步骤开始前解析所有写入路径，保护正式证据 |

## R1

**显式关闭 prefix caching，避免继承 engine 默认值。**

[vllm_contract.py](../src/vllm_contract.py) 原来的 `server_argv()` 在 false 时省略参数。
上游 [vLLM v0.10.2 EngineArgs](https://github.com/vllm-project/vllm/blob/v0.10.2/vllm/engine/arg_utils.py#L1617)
在 `_set_default_args_v1()` 中，对非 pooling 模型默认开启 prefix caching。这会使
cache-off baseline 实际复用缓存，并可能因多个 case 共用服务进程而影响结果。

修复后 true/false 分别生成 `--enable-prefix-caching` / `--no-enable-prefix-caching`，
字符串 `"false"` 被拒绝。启动前的 CLI help 校验、保存的 argv 和 verifier 都使用同一
显式开关。回归覆盖两个分支、错误类型和缺少该 CLI 能力时的拒绝行为。
实际 L4 engine 的运行配置仍需在 GPU 实验时确认，不能把 CPU 参数测试当成实测。

## R2

**统一声明窗口，并单独报告包含 drain 的吞吐。**

原 [analyze_week03.py](../src/analyze_week03.py) 使用 100 s 声明窗口，
[openai_trace_client.py](../src/openai_trace_client.py) 使用首个 measurement 到达至最后
terminal 的区间。100 个成功请求、首到达 20 s、最后结束 220 s 的合成例子，在两端
分别得到 1.0 和 0.5 rps，差异来自分母。

修复后两端共享 [week03_contract.py](../src/week03_contract.py) 的 `measurement_window()`，
默认测量窗口均为 20–120 s。summary/replay case 显式保存 start/end，吞吐为最终完成的
measurement 请求数除以声明窗口。另行计算：

```text
drain duration = max(声明窗口结束, 最后 terminal) - 测量窗口开始
drain throughput = 同一 completed count / drain duration
```

comparison gate 检查相同 trace/rate 的窗口一致、duration 与窗口一致、吞吐与完成数一致。
replay 恢复和最终 verifier 从逐请求记录重算，拒绝旧分母或被修改的汇总。adapter 版本
升级为 2.0。回归覆盖提前结束、长时间 drain、窗口不一致及被篡改的 replay 汇总。

该主指标仍包含窗口结束后才完成的请求，不等于窗口内部完成的实时吞吐；报告需要同时
解释 drain。跨周图当前画的是 TTFT，吞吐用于比较数据与报告。

## R3

**固定参数的负载曲线不受额外参数扫描影响。**

原 Week 3 图只按 policy/load 聚合，在 0.75 负载下混入 batch size/delay 扫描：两个
batching policy 各有 8 行，而其他负载只有默认参数的 3 repeats。Week 4 loader 还会
丢失 batch/delay 维度，因此相同问题进入跨周 TTFT 图。

修复后共享 `is_baseline_case()` 按 `matrix.policy_defaults` 筛选。55 个 primary case
保留 45 个用于主负载曲线，额外扫描仅用于参数图。
[Week 4 loader](../src/analyze_week04.py) 同样筛选，并保留 case ID、batch size 和 delay。
回归检查每个 policy/load 仍为 3 repeats，以及添加扫描点不会改变基线数据。

## R4

**推荐负载必须具备完整、全部达标的 repeats。**

原 `build_analysis_summary()` 会在 8 rps 的三个 repeats 仅一次达标时，仍选中该负载。
修复后先按 request rate 分组，要求 repeat 集恰好为配置的完整集合且每条结果都满足 SLO。
重复 repeat 直接报错，缺失 repeat 的组不入选。

分析保存 `operating_point_rule` 和 `operating_point_groups`；从全部达标的组中选最高负载，
以其中 TTFT 最差的 repeat 作为报告的具体证据行，并附带 repeat validation。
verifier 也重建并检查这些字段。回归确认“8 rps 一次通过、两次失败，4 rps 全通过”时
选择 4 rps，并检查缺失或重复 repeat 的处理。

## R5

**先确定完整输出路径，再执行日志、校准和 HF 前置步骤。**

原 [run_week03.sh](../scripts/run_week03.sh) 只把 `OUTPUT_ROOT` 传给 runner，日志、
freeze、snapshot 和 environment 仍使用正式路径；Make 的 calibration 前置依赖也先
写正式目录。

新增 [prepare_week03_run.py](../scripts/prepare_week03_run.py)，在任何前置步骤之前
生成 `run-config.json`。隔离根目录覆盖日志、环境、依赖、模型快照、raw、metadata、
status、summary、analysis、figures、report 和 receipt。Make 通过 `CALIBRATE=1`
在路径确定后生成 calibration，smoke 的 calibration 同样写入自己的目录。
未要求重新生成 calibration 时，它作为只读输入引用。

非正式根目录不能位于正式目录内，隔离运行的显式日志也必须留在自己的根目录。
回归预置正式证据文件，分别执行 fake shell 和替换硬件步骤的 HF Make 入口，核对正式
文件字节未变、所有前置产物落在隔离目录。HF 替身只验证编排与路径，不代表实际推理测试。

## R6

**Smoke 只验证自身具备的数据，不要求过载图。**

修复前 smoke 的 3 cases/60 events 完成后，完整分析报错：

```text
ValueError: Week 3 evidence has no overload events
```

修复后 smoke 生成 `summary.csv` 与 `analysis.json`，明确保存 `figures=[]`，跳过完整
性能绘图；primary/extended 保留原四张图。shell 的日志改为普通管道并保留 `pipefail`，
等待日志结束，避免 `/dev/fd` 重定向的兼容性问题。入口回归同时验证正常退出、正式证据
隔离，以及注入模型准备失败时保留非零退出码、日志和不执行后续 case。

## 中文文档与兼容性

- [Week 2](week-02-code-walkthrough.md)：模块地图、真实 batch loop、计时边界、KV 长度、
  显存开销、P95 样本层级、OOM 和恢复。
- [Week 3](week-03-code-walkthrough.md)：producer/coordinator/worker 分工、准入与队列、
  时间线、E2E 校准、统一窗口、输出隔离和 smoke 完成条件。
- [Week 4](week-04-code-walkthrough.md)：随机 matrix 与精确 replay、run/server attempt、
  SSE、queue 均值、完整 repeats 选点及离线验收。

课程周数、原实验矩阵与资源约束保持一致。新 summary 增加测量窗口字段，replay adapter
使用 2.0；旧汇总不能直接混入新 comparison。正式证据继续校验源码身份，修复前 GPU
证据应保留原始来源，不能仅换一份新报告就宣称来自修复后的实验。

## 验证

修复前 282 个测试通过，但未覆盖这些语义。新增回归位于
[test_week03_review_regressions.py](../tests/test_week03_review_regressions.py)、
[test_week03_week04_comparison.py](../tests/test_week03_week04_comparison.py)、
[test_openai_trace_client.py](../tests/test_openai_trace_client.py) 和
[test_vllm_contract.py](../tests/test_vllm_contract.py)。

Python 3.12.7 的全量回归为 **294 passed**。完整 primary CPU 模拟完成 55 cases，
再次运行新增 0、跳过 55；分析生成 55 条 summary 和四张图。Week 4 plan 正常并包含
显式关闭 prefix caching 的参数。本次 Python 文件的 Pyflakes、shell 语法和 diff
空白检查通过；使用本机已有工具，没有安装新依赖。

未执行 L4/CUDA 或 vLLM 实际启动。Week 2 未发现新的主流程阻断问题，其计时和变长输入
限制已在导读中说明。
