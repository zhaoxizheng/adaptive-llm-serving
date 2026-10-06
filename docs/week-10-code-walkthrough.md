# Week 10 中文代码导读：从 SchedulerOutput 到 GPU 执行

本周把 scheduler 的 token 决策与 runner 的真实 input shape 关联起来，观察 prepare、
forward、sampling、output copy 与 CUDA Graph replay。配套：[学习计划](week-10-plan.md)、
[参考资料](week-10-references.md)、[运行约定](week-09-12-runbook.md)、
[报告模板](../reports/week10.md)。

## 1. 代码地图

| 文件 | 阅读重点 |
|---|---|
| [week10.yaml](../configs/week10.yaml) | prefill、decode、mixed，以及 eager/graph A/B |
| [profiling-workloads.yaml](../configs/profiling-workloads.yaml) | 固定 token shape 与到达间隔，后两周复用 |
| [week10-execution-trace.patch](../patches/week10-execution-trace.patch) | SchedulerOutput 字段、runner 方法边界、真实 replay hook |
| [execution_trace_payload.py](../scripts/execution_trace_payload.py) | `schedule`、`execution`、`phase`、`shape`、`graph` |
| [parse_execution_trace.py](../src/parse_execution_trace.py) | scheduler/runner join、padding 和 graph 证据校验 |
| [run_deep_study.py](../scripts/run_deep_study.py) | 场景运行、client metrics、目标观察结果 |

源码地图见 [Worker / Model Runner map](vllm-worker-model-runner-map.md)，字段逐项解释见
[shape ledger](vllm-step-shapes.md)。

## 2. 为什么单独传递 study_step

Scheduler 和 GPU runner 可能位于不同进程。不能给每个进程各写一个从零开始的计数器，
就假定两边永远相同：没有 token 的 step、初始化 dummy run 都会让这种假设失效。

Patch 给 `SchedulerOutput` 增加默认值为 `-1` 的 `study_step`。启用时，scheduler
在 `_update_after_schedule()` 增加 computed token 计数之前保存本步 request composition，
把编号随真实 output 传给 runner。它不改变 token 分配或 model execution 的参数。

```text
schedule step=23: request A tokens=1, request B tokens=256
      ↓ SchedulerOutput.study_step=23
runner step=23: shape → forward → sample → output
```

初始化的模型加载、KV 初始化和 graph capture 没有请求 step，使用 `-1` 并与正式请求
ledger 分开。这些边界说明初始化路径，不能作为某个请求的 latency。

## 3. 核心代码精读

### 将不同请求本轮的 tokens 拼成一次模型输入

固定 vLLM `v0.10.2 / 01efc7ef781391e744ed08c3292817a773d654e6`，重点读
`GPUModelRunner._prepare_inputs()`，而不只看 `execute_model()` 的入口：

源码：[vllm/v1/worker/gpu_model_runner.py](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/v1/worker/gpu_model_runner.py#L883-L902)，第 883–902 行；以下为原文摘录，仅移除公共缩进。

```python
req_ids = self.input_batch.req_ids
tokens = [scheduler_output.num_scheduled_tokens[i] for i in req_ids]
num_scheduled_tokens = np.array(tokens, dtype=np.int32)
max_num_scheduled_tokens = max(tokens)

# Get request indices.
# E.g., [2, 5, 3] -> [0, 0, 1, 1, 1, 1, 1, 2, 2, 2]
req_indices = np.repeat(self.arange_np[:num_reqs],
                        num_scheduled_tokens)

# cu_num_tokens: [2, 5, 3] -> [2, 7, 10]
# arange: [0, 1, 0, 1, 2, 3, 4, 0, 1, 2]
cu_num_tokens, arange = self._get_cumsum_and_arange(
    num_scheduled_tokens)

# Get positions.
positions_np = self.positions.np[:total_num_scheduled_tokens]
np.add(self.input_batch.num_computed_tokens_cpu[req_indices],
       arange,
       out=positions_np)
```

`req_ids` 使用 persistent batch 的顺序，将 scheduler 的字典转换为同序数组。
`np.repeat` 为每个本轮 token 标明所属请求；累计和给出各请求在扁平输入中的边界；
`arange` 是请求内部的本轮偏移。加上各请求已计算长度，就得到真实 position。

手算本轮数量 `[2,1]`、已计算长度 `[4,7]`：`req_indices=[0,0,1]`，局部偏移
`[0,1,0]`，positions=`[4,5,7]`，query boundaries=`[0,2,3]`。普通采样分支从
每段末位 `[1,2]` 取 logits；它没有给每个请求都填成相同长度的 `[B,P]`。
接下来再跟 `token_indices` 和 `compute_slot_mapping()`，看逻辑 token 如何映射到
CPU token buffer 与物理 KV slots，不能把 position 直接当成物理显存地址。

### CUDA graph replay 复用的是执行图与固定 buffers

源码：[vllm/compilation/cuda_graph.py](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/compilation/cuda_graph.py#L181-L193)，第 181–193 行；以下为原文摘录，仅移除公共缩进。

```python

if self.is_debugging_mode:
    # check if the input addresses are the same
    new_input_addresses = [
        x.data_ptr() for x in args if isinstance(x, torch.Tensor)
    ]
    assert new_input_addresses == entry.input_addresses, (
        f"Input addresses for cudagraphs are different "
        f"during replay. Expected {entry.input_addresses}, "
        f"got {new_input_addresses}")

entry.cudagraph.replay()
return entry.output
```

wrapper 先按 mode 和 batch descriptor 选择已有 entry；无匹配时走普通调用或 capture。
这里 debugging 分支检查输入 tensor 的地址一致，随后 `replay()`，返回 entry 的输出。
输入内容每轮可以更新，但 capture 依赖的地址与 shape 条件必须保持一致；返回 buffer
也可能在下一次执行被覆写，不能把它当作每轮新建、可无限保留的输出快照。

**设计取舍与边界。** Graph padding 可以增加实际执行 shape，而不增加真实请求数。
`num_scheduled_tokens`、padded input size 与 sampled token 数是不同量。CPU 的 forward
range 主要标记提交边界；kernel 执行和同步等待仍需下一周的 GPU timeline。

**读后自检。** 把上例的 `req_ids` 顺序反转却不反转 computed lengths，会算出什么
错误 positions？只有启动阶段的 capture 日志，能否证明这一步发生 replay？

## 4. phase() 测量的是什么

`phase(name)` 同时产生 PyTorch `record_function`、NVTX range 和 CPU boundary event：

```python
label = f"study/{name}/step={step}"
with torch.profiler.record_function(label), torch.cuda.nvtx.range(label):
    yield
```

事件的 `cpu_duration_us` 使用本进程 `monotonic_ns()`。没有每步插入 CUDA synchronize，
所以这里保存的是 CPU 调用区间，包括可能发生的等待；异步 kernel 可以在此区间结束后
继续运行。`gpu_execute_us` 明确留空，后两周再从 CUDA activity 得到 GPU 时间。

标记覆盖 update_batch、prepare、preprocess、forward、logits、sample、output_copy 和
整个 execute。不同层的 range 会嵌套，不能把 execute 加上所有子 range 当作总时间。

## 5. Persistent batch 怎样变成模型输入

`_update_states()` 处理新请求、继续执行的请求与已完成请求，维护 request state 和
`InputBatch`；`_prepare_inputs()` 使用这些状态更新 positions、block table、slot
mapping 和 attention metadata；`_preprocess()` 最后选择实际传给 model 的 tensor slices。

`shape()` 只读维度与 CPU request 计数，不复制 input IDs、positions 或 logits 内容。
普通 text 模型的 input IDs 和 positions 为一维，但其长度可能含 graph padding。

```text
logical work = sum(num_scheduled_tokens.values())
prefill = sum(min(scheduled, max(0, prompt_tokens - computed_tokens)))
decode = logical work - prefill
padded input length >= logical work
```

Parser 用 scheduler 的更新前计数重新计算 composition，再与 runner 观察值对比。如果
图接受 8 tokens 而实际只有 5 tokens，`[8]` 是合法的 input shape，KV 有效 slots 仍为 5。

## 6. Graph dispatch 与 graph replay 分别取证

`dispatch_mode` 来自当前 dispatcher；`execution_mode` 根据 `CUDAGraphWrapper` 内部
真正执行的 capture/replay 事件确定：

| 证据 | `execution_mode` |
|---|---|
| dispatcher=NONE，且没有 graph event | eager |
| 在当前 step 调用了 `entry.cudagraph.replay()` | graph_replay |
| 当前 step 创建并记录了 captured graph | graph_capture |
| dispatcher 选择图，但 trace 无实际 capture/replay | unproven_graph |

Piecewise 模式可能一轮 model forward 触发多个 graph replay，parser 按 step 汇总，
不会把 event 数直接解释成 token 数。启动期间看到 graph capture 也不意味着本次 workload
一定命中了那些图；请求 step 必须有自己的 replay 证据。

## 7. 场景与读图顺序

Prefill 使用 128 input / 1 output，便于看一条最短执行路径；decode 使用 128 input /
256 output，读时剔除首个 prefill step。Mixed 在 decode 请求开始 100ms 后加入 2048-token
prompt，启用 chunked prefill 并设 token budget=256。

Eager 与 graph 使用同一个 decode workload，仅改变 `enforce_eager`。取消 eager flag
只是允许图路径，最终仍需检查 replay。比较时看相同逻辑 token/batch shape，padding 和
dispatch 差异另行解释；不能把不同 batch size 的两个 step 当作受控 A/B。

## 8. 命令与结果

在 Week 9 patch 后应用 Week 10 patch，再运行：

```bash
make plan-week10 PYTHON=python3.12
PYTHON=.venv-vllm/bin/python bash scripts/run_week10_execution_scenarios.sh --run --scenario mixed
```

结果在 `results/week10/traces/mixed/SESSION_ID/`。`capture/shapes.csv` 是最先阅读的
表；`capture/execution-PID.jsonl` 可回溯每个边界；`client.json` 和 server metadata
连接 HTTP 结果与实验身份。`observation.json` 说明是否实际出现 mixed/graph 等目标。

```bash
python3.12 -m src.parse_execution_trace \
  --trace-dir results/week10/traces/mixed/SESSION_ID/capture \
  --output results/week10/analysis/mixed
```

解析器要求每个有 token 的 scheduler step 都有 shape 和 output，拒绝重复 step、缺失
进程事件、composition 不一致，以及“eager dispatch 却出现 replay”的矛盾。

## 9. 自测与 Week 11 交接

先运行 `python3.12 -m pytest -q tests/test_deep_study.py`。理解为什么合法 padding 应
通过、为什么只有配置没有 replay 的 run 会保留 `unproven_graph`，再读 GPU trace。

给 Week 11 的三个候选假设可以是：long prefill 主要增加大矩阵/attention 工作；steady
decode 主要受短 kernels 与 host 开销影响；mixed step 的长 prefill 工作干扰 decode。
当前代码不预先接受这些假设，报告要用后续 operator 与系统 timeline 逐一验证。
