# Week 12 中文代码导读：Nsight Systems 与 CPU–GPU Timeline

本周复用 Week 11 workload 和 Week 10 NVTX，保存短窗口 `.nsys-rep`，导出 SQLite，
把 GPU busy/idle 与 CPU 调用证据放在同一个 timeline 中。配套：[学习计划](week-12-plan.md)、
[参考资料](week-12-references.md)、[运行约定](week-09-12-runbook.md)、
[报告模板](../reports/week12.md)。

## 1. 代码地图

| 文件 / 函数 | 作用 |
|---|---|
| [week12-nsys.yaml](../configs/week12-nsys.yaml) | domains、capture window、四个场景 |
| [run_week12_nsys.sh](../scripts/run_week12_nsys.sh) | 命令入口 |
| [run_deep_study.py](../scripts/run_deep_study.py) `nsys_prefix` | 版本检查、Nsight 启动参数、capture/export |
| [execution_trace_payload.py](../scripts/execution_trace_payload.py) `Capture` | GPU 进程内的 CUDA profiler start/stop 与 NVTX |
| [summarize_nsys.py](../src/summarize_nsys.py) | 只读 SQLite、区间合并、gap 表、SVG timeline |
| [compare_execution_runs.py](../src/compare_execution_runs.py) | eager/graph 的工作负载、环境、逻辑 shape 和 replay 证据检查 |

## 2. 为什么不用“server 启动后延迟 30 秒”

模型加载、编译、graph capture 用时会变化，固定墙钟 delay 容易捕获初始化或空闲。
这里由实际 GPU runner 的 engine step 控制窗口：warmup 完成、客户端 arm 后，数完
wait/warmup steps，再执行 CUDA profiler API start；active steps 结束后 stop。

实际 Nsight 前缀采用：

```text
nsys profile
  --trace=cuda,nvtx,osrt --sample=none --cpuctxsw=none
  --trace-fork-before-exec=true
  --capture-range=cudaProfilerApi --capture-range-end=stop
  --wait=all --force-overwrite=false --output=.../profile
  <vllm binary> serve ...
```

`nsys_prefix()` 先读取本机 `profile --help`，不匹配所需能力就拒绝运行。记录 fork
路径是因为 API 和 Engine Core 可能使用子进程；真正的 GPU 证据仍以报告中出现的
CUDA kernel/API 为准。

为了裁清窗口，在 start 前和 stop 前各做一次 CUDA synchronize。这两次在窗口边界，
不是在每个 model step 插同步；但仍可能扰动运行，所以每场景都保留无 capture baseline。
采集完成后通过自身进程组的 SIGINT 清理并给 nsys 留出导出时间；缺少 `.nsys-rep`
时采集报错，保留 server log，不把空文件视为成功。

## 3. NVTX 如何连接 scheduler、CPU 和 GPU

名称为 `study/prepare/step=17` 的 NVTX range 位于执行 prepare 的 CPU 线程。
相同 step 编号来自 scheduler，经 `SchedulerOutput` 传到 runner，`shapes.csv` 给出
这个 step 的 prefill/decode composition。

CPU NVTX 与 GPU kernel 不是天然一一嵌套。需要同时查看 CUDA API 的 correlation ID、
stream、kernel 起止和 CPU range；API 返回后 GPU work 仍可排队执行。SQLite 原始字段
保留 `correlation_id`、`global_tid`、device 和 stream，便于回到 GUI 继续验证。

## 4. SQLite reader 怎样适应 schema 差异

`read_timeline()` 以 SQLite `mode=ro` 打开文件，枚举实际表，读取：

```text
StringIds
CUPTI_ACTIVITY_KIND_KERNEL / CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL
CUPTI_ACTIVITY_KIND_RUNTIME
CUPTI_ACTIVITY_KIND_MEMCPY / MEMSET
NVTX_EVENTS
```

名称可以在表内直接保存，也可以通过 `demangledName`、`shortName`、`nameId` 或
`textId` 查 StringIds。Reader 支持这些常见布局；未知布局缺少 kernel/API/step range
时明确报错，保留原始导出以便适配，不用“0 个 kernel”掩盖 schema 问题。

Nsight SQLite 时间是纳秒，输出字段都用 `_ns` 后缀；PyTorch Chrome trace 是微秒。
不能直接拿两个文件的 timestamp 相减。跨工具用 workload 身份、step、shape 和范围
名称建立对应，而不是假设两个独立进程启动具有相同绝对时钟。

## 5. Busy/idle 为什么要做 union

窗口由捕获到的第一个 `study/execute` 开始和最后一个 execute 结束限定。
Kernel、memcpy、memset 区间先裁到窗口，再合并跨 stream 的重叠部分：

```python
busy = union_intervals(kernel_intervals + copy_intervals + memset_intervals)
gpu_busy_ns = sum(end - start for start, end in busy)
gpu_idle_ns = window_end - window_start - gpu_busy_ns
```

例如 kernel A 为 20–60ns，kernel B 为 40–80ns，busy 是 20–80ns 的 60ns。直接加
duration 会得到 80ns，夸大实际占用。Reader 拒绝多 GPU 混算，避免把多个设备的
并发 activity 合成一张单卡 busy 图。

这里 busy 表示该设备有观测到的 CUDA activity，不等于 SM occupancy、算力利用率
或内存带宽利用率。后者需要更多 counters，留给 Week 13。

## 6. Gap 的分类为什么默认 unknown

`gaps.csv` 给出每段空闲区间，附重叠的 CUDA API/NVTX 名称。它默认写 `unknown`。
重叠是定位线索，不能单独证明因果，例如 CPU 在 prepare 时 GPU 可能仍在等先前事件。

| 报告分类 | 还需要核对的证据 |
|---|---|
| Host launch gap | CPU 正在逐个提交、GPU queue 暂时为空，核对 correlation/stream |
| Synchronization | sync API 或 blocking copy 与 GPU 完成/等待关系 |
| Input preparation | prepare range 期间尚未提交下一批 GPU work |
| Graph dispatch | 真实 cudaGraphLaunch/replay 与前后空隙 |
| Workload bubble | scheduler 没有足够 runnable work，核对 step/composition 与 client 到达 |
| Unknown | 当前 domains 或队列证据不足，说明缺什么 |

脚本同时生成带类别、相对毫秒刻度和悬停事件名的 SVG。它帮助定位时间区域，不替代
Nsight GUI 的完整线程、stream 和依赖分析；主要结论仍在报告中引用原 `.nsys-rep`。

## 7. Eager/graph A/B 怎么检查

`eager_decode` 与 `graph_decode` 共用 128 input / 256 output，只改变 enforce-eager。
均在 wait=2、warmup=2 后捕获 16 steps。采集后运行：

```bash
python3.12 -m src.compare_execution_runs \
  --eager results/week12/nsys/eager_decode/EAGER_SESSION \
  --graph results/week12/nsys/graph_decode/GRAPH_SESSION \
  --output results/week12/tables/graph-comparison.json
```

Comparator 验证 workload ID、源码、tokenizer、GPU/software、其他 CLI 参数一致，
active steps 的逻辑 shape 分布一致，左侧实际 eager、右侧实际 replay。Padding shape
分别保存供解释；图的 padding 可以不同，但不能悄悄改变真实工作量。

通过检查后仍要看 `pair.json` 的 capture overhead。只观察到允许 graph 的配置、没有
真实 replay，或者混入不同 batch shape，都会拒绝形成有效比较。

## 8. 命令与离线导出

```bash
make plan-week12 PYTHON=python3.12
PYTHON=.venv-vllm/bin/python bash scripts/run_week12_nsys.sh --run --scenario long_prefill
```

session 在 `results/week12/nsys/long_prefill/SESSION_ID/`，保存 baseline/capture、
pair、observation 和 export command。`capture/profile.nsys-rep` 是原始报告，
`capture/profile.sqlite` 是脚本导出。表在 `results/week12/tables/long_prefill/SESSION_ID/`，
图在 `results/week12/figures/long_prefill/SESSION_ID.svg`。

```bash
python3.12 -m src.summarize_nsys \
  --sqlite results/week12/nsys/long_prefill/SESSION_ID/capture/profile.sqlite \
  --output results/week12/tables/long_prefill/reanalysis \
  --figure results/week12/figures/long_prefill/reanalysis.svg
```

Mac 的离线摘要不需要安装 Nsight 或 CUDA。若需重新从 `.nsys-rep` 导出 SQLite，则在
有兼容版本 nsys 的机器使用保存的 `export-command.json`。

## 9. 交给 Week 13 的内容

先复核 Week 11 三个假设，记录支持、推翻或仍不确定。然后选择一个具体 kernel 或
kernel family，附上所在 step、shape、调用次数、耗时范围与选择原因。提出待测 counter
问题，例如计算吞吐、memory bandwidth、occupancy 或 launch 配置，而不是只抄一个
“耗时第一”的 kernel 名称。同步原始结果并停止 VM 后，才完成本周运行闭环。
