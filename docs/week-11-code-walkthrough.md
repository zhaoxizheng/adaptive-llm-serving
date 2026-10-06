# Week 11 中文代码导读：PyTorch Profiler 的窗口与算子摘要

本周把 Week 10 的执行假设转成短窗口 profile。先收集相同 workload 的无 profiler
baseline，再采集 CPU+CUDA activity，保留两者的客户端指标与差异。配套：
[学习计划](week-11-plan.md)、[参考资料](week-11-references.md)、
[运行约定](week-09-12-runbook.md)、[报告模板](../reports/week11.md)。

## 1. 代码地图

| 文件 / 函数 | 作用 |
|---|---|
| [week11-profiler.yaml](../configs/week11-profiler.yaml) | 四场景、模型预热次数、wait/warmup/active 和开销选项 |
| [run_week11_profiler.sh](../scripts/run_week11_profiler.sh) | `--plan` / `--run` / `--scenario` 入口 |
| [run_deep_study.py](../scripts/run_deep_study.py) `run_one` | 分别启动 baseline 与 profile 服务，完成预热后 arm |
| [execution_trace_payload.py](../scripts/execution_trace_payload.py) `Capture` | GPU runner 进程中的 profiler 生命周期与导出 |
| [summarize_torch_profile.py](../src/summarize_torch_profile.py) | 无需 torch 的离线 CPU/CUDA/operator/memory 摘要 |
| [profile_tables.py](../src/profile_tables.py) `paired_metrics` / `union_intervals` | 成对 workload 校验、开销比例、重叠区间合并 |

## 2. 为什么 profiler 放在 runner 里

API 进程发起一次请求，并不意味着该进程执行了 GPU 算子。只 profile HTTP 客户端会
看到网络等待，却看不到模型计算。本实现复用 Week 10 patch，让 `Capture` 在实际
`GPUModelRunner.execute_model()` 所在进程创建 PyTorch profiler。

启动阶段不直接开始 profile。Runner 先服务三轮相同 workload，预热模型与常见 shape，
客户端再创建 `armed` 文件；worker 在下一次有 token 的 execute 时进入 profiler schedule。

```text
model load / graph capture
    → 3 workload warmup rounds
    → armed file
    → wait → profiler warmup → active → export → done
```

外部模型预热和 profiler 的 `warmup` 不同：前者稳定模型路径，后者让 profiler 准备
采集设施而不保留该阶段最终 trace。一次普通请求可以跨多个 schedule steps。

## 3. Capture.before / after 怎样配合

装饰器先设置收到的 `study_step`，再调用 `Capture.before()`。首次 arm 后，创建：

```python
torch.profiler.profile(
    activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
    schedule=torch.profiler.schedule(wait=wait, warmup=warmup, active=active, repeat=1),
    record_shapes=True,
    profile_memory=True,
    with_stack=False,
    on_trace_ready=export,
)
```

每个有 token 的 execute 返回后调用一次 `profiler.step()`。最后一个 active step 后
导出 trace，停止 profiler，写 `capture-PID.json`，记录确切的 `active_steps`。客户端
runner 必须看到完整 marker 才接受采集完成；请求提前结束导致步数不够不会被伪装成
一个完成的窗口。

`record_shapes` 和 `profile_memory` 带来开销；`with_stack` 默认关闭。短 prefill 只有
一个执行 step，因此采用 wait=0/warmup=0/active=1，依赖前面的三轮模型预热；它仍有
profiler 首次采集开销，必须读 paired baseline，不能把这次样本当作准确延迟基线。

## 4. 核心代码精读

### Profiler 的时钟是 execute step，不是 HTTP request

`Capture.before()` 判断当前 index 是否位于 active 窗口，`after()` 在一次实际执行
之后推进 index。关键结束逻辑如下：

源码：[scripts/execution_trace_payload.py](../scripts/execution_trace_payload.py)，第 199–217 行；以下为原文摘录，仅移除公共缩进。

```python
def after(self):
    import torch

    self.index += 1
    if self.profiler is not None:
        self.profiler.step()
    if self.index == self.cfg["wait"] + self.cfg["warmup"] + self.cfg["active"]:
        if self.profiler is not None:
            self.profiler.stop()
        else:
            torch.cuda.synchronize()
            torch.cuda.profiler.stop()
        self.done = True
        root = Path(self.cfg["output"])
        (root / f"capture-{os.getpid()}.json").write_text(
            json.dumps(
                dict(complete=True, active_steps=self.steps, config=self.cfg, pid=os.getpid())
            )
        )
```

先加一再 `profiler.step()`，使这次完成的 model execution 落入对应 schedule step。
当 index 等于 `wait + warmup + active` 时才停止并写 complete marker。
`active_steps` 保存 scheduler 传来的 step ID；它不等于从 0 开始的 capture index。

手算 wait=2、warmup=2、active=3：进入 execute 前的 index 0、1 为 wait，2、3 为
profiler warmup，4、5、6 为 active；第 7 次执行后的 index=7 才完成。若 workload
只有 5 个 execute，marker 不完整是正确结果，不能靠 HTTP 请求全部成功推断采集完成。

继续读装饰器 `execution()`：只有被包装方法正常返回后才调用 `after()`；`finally`
负责恢复 step context。forward 抛异常时不会用一个递增计数掩盖失败。完整 marker
仍不能单独证明每个客户端请求成功，必须与 client 结果和 active step 组成一起判断。

**设计取舍与边界。** `record_shapes` 有利于把同名算子按输入拆开，但可能增加开销和
对象存活时间；`self_cpu_time_total` 排除子调用，`cpu_time_total` 包含它们。不能把所有
total 列累加成请求墙钟时间，也不能仅凭 CPU 的大耗时行认定 GPU kernel 慢。

**读后自检。** 一个输出 256 tokens 的请求为什么可能覆盖很多 profiler steps？
如果 mixed 请求在 active 窗口结束后才被调度，怎样从 `active_steps` 识别无效采样？

## 5. 四个窗口如何选

| 场景 | 输入 / 输出 | wait / warmup / active | 验证重点 |
|---|---|---|---|
| short_prefill | 128 / 1 | 0 / 0 / 1 | 一步内完整 prefill 路径 |
| long_prefill | 2048 / 1 | 0 / 0 / 1 | 大 shape 的 operator composition |
| decode | 128 / 256 | 2 / 2 / 16 | 丢弃开头，窗口内应全为 decode |
| mixed | decode 后 100ms 到达长 prompt | 0 / 1 / 64 | 窗口内实际是否有 mixed step |

这些是固定起点，不保证所有 operating point 的时序相同。`observations()` 只在
`active_steps` 中判断目标机制；mixed 没落在窗口里时结果为 `not_observed`，需要记录
原因、调整一个窗口或到达参数后重新采集，不能改名称掩盖窗口未命中。

## 6. 导出两份互补证据

`Capture.export()` 保存 `torch-PID.json` Chrome trace，同时直接从 PyTorch
`key_averages(group_by_input_shape=...)` 导出 `operators-PID.json`。

| 字段 / 证据 | 含义 |
|---|---|
| `self_cpu_us` | operator 的 CPU self time，排除嵌套子调用 |
| `cpu_total_us` | 包含子调用的 CPU 累计时间 |
| `self_device_us` / `device_total_us` | PyTorch 关联到 operator 的 device 时间 |
| raw trace 的 `kernel` | 真实 CUDA kernel 区间，可能跨 stream 重叠 |
| `cuda_runtime` | CPU 端 CUDA API 时间，可能包含提交或同步等待 |
| `[memory]` / memory usage 字段 | allocation/free 或累计内存变化，单位 bytes |

离线脚本使用导出的 operator self time，不从嵌套 Chrome events 盲目相减重造它。
kernel busy time 则合并重叠区间后求长度；两条 0–10us、5–15us 的 kernels 并发时，
union busy 是 15us，不是 20us。即便这样，busy 也不等于 request latency。

`synchronization.csv` 提取名称含 `Synchronize` 的显式 CUDA API。隐式 blocking copy、
CPU tensor 读取等仍需看完整 trace，不能因为该表为空就宣布没有同步。

## 7. Baseline/profile pair 怎样验证

两次服务使用同一 command、GPU/software runtime、jobs 和 seed。`paired_metrics()`
先比较 request ID、prompt fingerprint 和 output token 数，失败或不匹配的 run 不能
形成有效 pair。

```text
overhead_ratio = captured_workload_wall_seconds / baseline_workload_wall_seconds - 1
```

`pair.json` 同时保存平均 TTFT、平均 TPOT、输出 tokens/s 和 wall time。单 token 输出
没有 TPOT，使用 `null`。wall time 包含 profiler 导出可能带来的等待，这正是测量
instrumentation 扰动的一部分。一次短 pair 的比例可能受随机波动影响，关键结论需要
在新进程中重复。

无 profiler baseline 没有详细 step trace，不能仅靠开销比例证明 batching 完全不变。
应结合 Week 10 的同 workload composition 与低频 metrics 核对；扰动明显或证据不足
时，仅用 profile 解释路径，不据此给生产耗时占比或容量结论。

## 8. 命令和文件

已完成 Week 10 patch 的 VM：

```bash
make plan-week11 PYTHON=python3.12
PYTHON=.venv-vllm/bin/python bash scripts/run_week11_profiler.sh --run --scenario decode
```

session 在 `results/week11/profiles/decode/SESSION_ID/`：`baseline/`、`capture/` 各有
client 与 server 记录，根目录有 `pair.json`、`run.json` 和 `observation.json`。
摘要表在 `results/week11/tables/decode/SESSION_ID/`。

```bash
python3.12 -m src.summarize_torch_profile \
  --trace results/week11/profiles/decode/SESSION_ID/capture/torch-PID.json \
  --operators results/week11/profiles/decode/SESSION_ID/capture/operators-PID.json \
  --output results/week11/tables/decode/reanalysis
```

该离线命令只需要普通 Python。原始 trace 可以在支持 Chrome trace 的查看器中打开，
使用 `study/prepare/step=N`、`study/forward/step=N` 等标记与 `shapes.csv` 对齐。

## 9. 三个假设怎样写结论

每个假设记录 Prediction、operator/trace evidence、baseline overhead、Decision 和
反证。Decision 只能来自本次采集，可为 supported/rejected/inconclusive。

Prefill 重点看增加了哪些算子与 shape；decode 看短 kernel、sampling 和 host 空隙；
mixed 看相同窗口里的 scheduler composition。Week 12 再用 Nsight 判断这些空隙对应
哪些 CUDA API、CPU ranges 与 GPU intervals，而不是只凭 operator 排名认定瓶颈。
