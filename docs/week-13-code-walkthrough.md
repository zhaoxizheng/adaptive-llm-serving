# Week 13 中文代码导读：Kernel Profiling 与 Prefix Caching

本周有两条独立数据路径：ncu 用于解释热点 kernel，普通服务请求用于测量 APC 收益。
对应 [学习计划](week-13-plan.md)、[参考资料](week-13-references.md)、
[运行约定](week-13-16-runbook.md)、[报告模板](../reports/week13.md)。

## 1. 从哪些文件读起

| 文件 / 函数 | 要理解的问题 |
|---|---|
| [week13-kernel.yaml](../configs/week13-kernel.yaml) | 选哪个 kernel、为什么选、允许多少 launches？ |
| [run_kernel_study.py](../scripts/run_kernel_study.py) `ncu_command/main` | 如何约束 capture、保存失败、导出 report？ |
| [summarize_ncu.py](../src/summarize_ncu.py) `parse_counters` | 如何保持 launch、metric 与单位对应？ |
| [week13-prefix.yaml](../configs/week13-prefix.yaml) | A/B/C/D 改变哪些变量？ |
| [serving_experiment.py](../src/serving_experiment.py) `trace_jobs` | 如何生成长度准确、可复现的 token 前缀？ |
| [run_serving_experiment.py](../scripts/run_serving_experiment.py) `run_cell` | 新进程、两种 warmup、测量与清理的顺序是什么？ |
| [experiment_client.py](../src/experiment_client.py) `run_load/request` | 如何保留失败并限制客户端并发？ |

## 2. Kernel target 为什么默认未填写

`week12_evidence`、`kernel_regex`、`shape`、`hypothesis` 都必须来自 Week 12 的实际
timeline。代码生成时无法知道你的真实热点。`--plan` 展示占位值；`--run` 拒绝缺失值。

`ncu_command()` 使用 `--target-processes all` 跟踪 worker，使用
`--profile-from-start off` 避开模型加载，并限制 `launch_skip/count`。实际 capture
由 Week 10 worker instrumentation 的 CUDA profiler start/stop 控制，而不是等固定秒数。
这里复用 instrumentation 中名为 `nsys` 的 CUDA API 窗口模式，没有同时启动 nsys。

先做一个 launch 的 `SpeedOfLight` smoke，再按假设增加本机支持的 sections。
脚本保存 ncu version/help/sections/metrics；没有 counter 权限时 `run.json` 标记
`blocked_counter_permission`，不会自行修改驱动权限。

默认 kernel replay 会重放目标 kernel，`cache_control=all` 和 `clock_control=base`
可能改变条件。报告必须保存实际 passes、单位和 clock/cache 影响。外部 HTTP 驱动的
server 无法直接复现 application replay，因此 runner 明确拒绝该模式。

## 3. Counter reader 不做什么推断

`parse_counters()` 跳过 CLI 前导消息，寻找 long CSV 的 `Metric Name/Metric Value`。
每行保留 process、launch ID、device/context/stream、kernel、grid/block、section 和 unit。
`1,234` 转成数值 1234，`N/A` 保留为 `raw_value`，不会改写成 0。

两个不同 launch 的 duration 不能直接平均后声称容量提升；低 occupancy 也不会触发
自动“瓶颈”结论。请把 counter 与原 shape、timeline 和可反驳的假设一起解释。

## 4. A/B/C/D 如何共用请求

`trace_jobs()` 分开管理到达随机数、family prefix 随机数和 suffix 随机数。
相同 seed/repeat/rate 产生相同到达序列；不同 repeat 使用不同到达序列。
传给 `/v1/completions` 的是合法 token ID 数组，所以输入长度不会因文本重新编码漂移。

| Case | APC | 初始状态 | 输入 |
|---|---|---|---|
| A_off | 关闭 | 新进程 | shared families |
| B_cold | 开启 | 模型已 warm，测量 prefix 未 priming | 与 A 相同 |
| C_warm | 开启 | 逐 family 预填充 | 与 A/B 相同 |
| D_off / D_on | 关闭 / 开启 | 新进程 | 首 token 逐请求不同，长度保持一致 |

共享前缀的长度是 512 tokens；同 family 的前 512 tokens 一致，后续 suffix 独立。
模型 warmup 使用保留的另一个首 token，和测量集不共享首个 block。D 使用原生 token
completion，不经过 chat template，因此这里能控制首 token 不同；换成 chat completion
时必须重新测量模板公共 tokens，不能沿用“零公共前缀”结论。

## 5. `run_cell()` 的执行顺序

```text
创建唯一 cell 目录，写 run.json / trace.json
  → 新 vLLM 进程，验证 source/GPU/help
  → 模型 warmup（不共享测量 prefix）
  → C 场景 prefix priming（记录时间与 token 成本）
  → metrics-before
  → 无 profiler open-loop 请求 + GPU/metric series
  → metrics-after / client.jsonl / summary.json
  → 清理自己启动的进程组，记录 GPU-seconds 与状态
```

B 的 cache 会在运行中变暖。`metric-series.jsonl` 保存每秒原始 cache/queue/preemption
exposition，`client.jsonl` 保留到达/首内容时间。离线 TTFT 图按到达顺序排列，结合
counter 时间序列判断 cold→warm，而不是把整个 B run 称为全冷。

`counter_delta()` 先逐 series 对比，再求和。任何一个 rank counter 回退都会标为 reset，
不会被另一 rank 增长抵消。metric 名称与 token/block/request 单位必须按固定版本确认；
配置 `definition` 为空时，摘要明确标记为未验证，不能直接发布 hit ratio。

## 6. 请求统计如何处理失败

客户端必须收到内容、正确 usage、`finish_reason=length` 和 `[DONE]` 才判成功。
线程池前有 semaphore；超过 `max_inflight` 的到达保存为 `client_overload`，不会无限排队
再假装按时发送。所有失败留在 error rate 与 SLO attainment 分母中。

`goodput = 成功且同时满足 TTFT/TPOT SLO 的请求数 / 测量及 drain 窗口秒数`。
客户端从预定到达计算 TTFT，还保存 arrival lag；客户端先饱和的 run 被拒绝用于容量结论。
原始输出只有合成数据，保留 hash/有限摘要供质量复核。

## 7. 命令与结果

```bash
make plan-week13 PYTHON=python3.12
# 在准备好的 GPU VM 中，先填 kernel target：
PYTHON=.venv-vllm/bin/python bash scripts/run_week13_ncu.sh --run
# 先跑一个低负载 cache cell；仍保留三个重复：
PYTHON=.venv-vllm/bin/python bash scripts/run_week13_prefix.sh --run --rate low --case B_cold
python3.12 -m src.analyze_serving_study --root results/week13/prefix --output results/week13/tables
python3.12 -m src.summarize_ncu --csv results/week13/ncu/SESSION/raw.csv --output results/week13/ncu/SESSION/tables
```

ncu 结果在 `results/week13/ncu/SESSION/`；APC 结果在
`results/week13/prefix/SESSION/rREPEAT-RATE-shared-CASE/`。没有实际执行时，这些目录不会
由文档生成过程制造出来。

## 8. 交给 Week 14

交接热点假设、raw `.ncu-rep`、无 profiler baseline、cache 初始状态、近 SLO 到达速率，
以及一个值得验证的 engine 参数。机制证据与用户延迟证据分别写入报告；没有性能收益
也可以形成有效结论，缺 counter 则保持未完成。
