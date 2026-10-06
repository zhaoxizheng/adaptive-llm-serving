# Week 7 代码导读：从 OpenAI API 到 Engine Core

本周围绕一条请求阅读 vLLM V1：请求怎样进入 API、转成内部对象、跨进程进入 Engine
Core，再把输出返回客户端。代码包含可应用的 trace patch、三条请求路径、事件断言和
时序图生成器。固定 revision 的源码已核对，GPU runtime trace 仍需在 VM 上执行。

配套阅读：[学习计划](week-07-plan.md)、[参考资料](week-07-references.md)、
[source map](vllm-source-map.md)、[请求生命周期](vllm-request-lifecycle.md)、
[报告模板](../reports/week07.md)。

## 1. 先固定“读的代码”和“跑的代码”

[vllm-revision.txt](../vendor/vllm-revision.txt) 固定：

```text
release = v0.10.2
commit  = 01efc7ef781391e744ed08c3292817a773d654e6
```

这与前几周 `requirements-vllm.txt` 的版本一致。它是阅读基线，不代表当前 Mac 已安装
或已经运行 vLLM。`runtime_status=not_executed` 明确保留这个区别；正式运行的 package
path、version 和 source commit 存在每次 trace 的 `source.json`。

`verify_source()` 不仅看版本号，还检查四个被修改的源文件和 trace helper 是否等于
本仓库生成的内容，并用 `inspect.getfile(vllm)` 验证实际 import 的 checkout。这样可以
发现“patch 打在 A 目录，服务仍运行 wheel B”的常见问题。
Editable build 的 package metadata 可能带 local/dev suffix；源码实验按固定 commit、
patch 内容和 import path 验证，并保存实际版本字符串。Week 5/6 仍要求发行版版本完全匹配。

## 2. 代码阅读地图

| 文件 / 函数 | 作用 |
|---|---|
| [week07.yaml](../configs/week07.yaml) | source checkout、Week 6 operating point、timeout、trace 上限 |
| [build_trace_patches.py](../scripts/build_trace_patches.py) `build` | 从固定 Git commit 的原始文件生成两周 patch |
| [week07-request-trace.patch](../patches/week07-request-trace.patch) | 在 API、AsyncLLM、Engine Core、scheduler 释放点添加事件 |
| [trace_payload.py](../scripts/trace_payload.py) `emit` | 被复制到 `vllm/_study_trace.py` 的结构化日志 helper |
| [run_source_study.py](../scripts/run_source_study.py) `lifecycle_requests` | nonstream、stream、abort 和 validation failure 请求 |
| [parse_request_trace.py](../src/parse_request_trace.py) `validate_path` | request ID、进程边界、取消传播和资源释放断言 |

推荐先读 source map，再看 patch 的每个 hunk 对应哪个边界，最后读 runner 和 parser。
不需要从 vLLM 仓库第一层目录依次往下读。

## 3. 核心代码精读

### 先登记输出接收者，再把请求交给 Engine Core

以下摘录固定 vLLM `v0.10.2 / 01efc7ef781391e744ed08c3292817a773d654e6`。
`AsyncLLM._add_request()` 是 API 侧进入 Engine Core 前的重要顺序约束：

源码：[vllm/v1/engine/async_llm.py](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/v1/engine/async_llm.py#L308-L318)，第 308–318 行；以下为原文摘录，仅移除公共缩进。

```python
async def _add_request(self, request: EngineCoreRequest,
                       prompt: Optional[str],
                       parent_req: Optional[ParentRequest], index: int,
                       queue: RequestOutputCollector):

    # Add the request to OutputProcessor (this process).
    self.output_processor.add_request(request, prompt, parent_req, index,
                                      queue)

    # Add the EngineCoreRequest to EngineCore (separate process).
    await self.engine_core.add_request_async(request)
```

`OutputProcessor` 在当前进程建立 request 与 collector 的关联；之后才 await IPC
提交。这样 Engine Core 返回结果时，本地已有对应的接收者。这里的 await 是异步提交
边界，既不意味着模型已经执行，也不意味着正在 API 进程执行 GPU forward。

沿 `generate()` 继续读：它从 `q.get_nowait() or await q.get()` 取结果，按
`out.finished` 决定结束。已有结果优先直接取，空队列才让出 coroutine；stream 与
nonstream 共用 engine 输出，但 serving 层消费和封装方式不同。取消分支捕获
`CancelledError/GeneratorExit`，调用 `abort(request_id)` 后重新抛出。

### Engine Core 的一次 step 不是处理完一个请求

源码：[vllm/v1/engine/core.py](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/v1/engine/core.py#L287-L299)，第 287–299 行；以下为原文摘录，仅移除公共缩进。

```python
# Check for any requests remaining in the scheduler - unfinished,
# or finished and not yet removed from the batch.
if not self.scheduler.has_requests():
    return {}, False
scheduler_output = self.scheduler.schedule()
model_output = self.execute_model_with_error_logging(
    self.model_executor.execute_model,  # type: ignore
    scheduler_output)
engine_core_outputs = self.scheduler.update_from_output(
    scheduler_output, model_output)  # type: ignore

return (engine_core_outputs,
        scheduler_output.total_num_scheduled_tokens > 0)
```

`schedule()` 产生本轮所有请求的工作量；executor 执行整个 `SchedulerOutput`；
`update_from_output()` 再把 sampled tokens、停止状态和资源释放反馈给 scheduler。
一个请求可能跨许多轮，一个 step 也可以包含多个请求。将这三个调用之间的对象画出来，
就能把“HTTP 请求生命周期”与“GPU 迭代周期”分开。

**设计取舍与边界。** 这是同步 step 主线，固定版本另有 batch queue/异步执行路径。
request ID 负责输出关联，PID/IPC 负责定位进程；类名不等于独立进程。客户端连接关闭
只能说明入口结束，还要继续核对 abort 到达 Engine Core、请求变为 terminal 和 KV free。

**读后自检。** 输出接收者若在 IPC 提交后才注册，会出现什么时序风险？为什么一个
stream 中已经返回首 token，仍不能删除 OutputProcessor 的 request state？

## 4. Patch 怎样做到可重建

`build()` 用 `git show <固定 commit>:<文件>` 读原始内容，不依赖工作目录是否已打 patch。
每处变更通过明确的代码 anchor 定位；anchor 不唯一或不存在就报错。`patch_text()`
产生 Git unified diff，新文件有明确的 `new file mode`。

Week 7 patch 不复制 scheduler 算法，只增加观察点。Week 8 patch 基于 Week 7 的结果
生成，因此应用顺序固定。可以在有源码 checkout 的机器上重建：

```bash
python3.12 -m scripts.build_trace_patches --source vendor/vllm
```

源码入口和行号以未修改的固定 commit 为准；patch 本地新增行会改变 checkout 行号，
阅读笔记仍应引用 commit permalink 中的原函数。

## 5. Request ID 怎样对应

本实验使用 `/v1/completions`，客户端给出 `request_id`，vLLM serving 层生成：

```text
客户端 r                 → API response ID: cmpl-r
单个 prompt 的 engine ID → cmpl-r-0
```

`_base_request_id()` 还支持 `X-Request-Id` header，优先于默认 ID；这里不同时提供两种
来源。`validate_path()` 用 parent ID 与单 prompt 内部 ID 收集同一条请求的事件。
批量 prompts、`n>1` 和多模型不属于这份最小实验，不能把这个 suffix 规则泛化到所有
vLLM 使用方式。

## 6. Trace event 对应什么真实行为

| Event | 位置与含义 |
|---|---|
| `request_received` | serving completion 已通过前置检查并生成 ID；不是原始 TCP/HTTP 入站时刻 |
| `ipc_submit` | AsyncLLM 注册 output collector 后，准备调用 Engine Core client |
| `core_received` | Engine Core 收到 request，即将加入 scheduler |
| `request_queued` | request 已放入 waiting，并登记到 requests 字典 |
| `core_output` | Engine Core 完成这一轮执行和结果更新，准备把 output 返回 |
| `async_output` | API 进程中的异步 collector 取到 RequestOutput，交给 serving 层 |
| `http_chunk` | serving 层准备序列化并 yield 普通内容 chunk |
| `response_completed` | nonstream response object 构造完成，即将返回 |
| `abort_sent` / `abort_received` | 取消进入 AsyncLLM / Engine Core |
| `request_freed` | scheduler 实际释放 blocks 并删除 requests 字典中的条目 |

有些事件发生在函数入口，有些发生在操作完成后，不能仅凭事件名做微秒级性能归因。
本 patch 的 `core_output` 位于普通 `EngineCore.step()`；主实验限定单 GPU、同步执行路径，
不支持用它证明 batch-queue、async scheduling、TP/PP 的完整输出链路。

worker/model runner 的执行入口在 source map 中指出，但本周没有在 CUDA kernel 或 worker
内部加采样。worker 是否独立进程取决于 executor，不能仅凭图中一个方框就说多一个 PID。

## 7. Nonstream 和 stream 的共享与分叉

两种请求共享 validation、preprocessing、AsyncLLM、Engine Core 和模型执行。serving
层决定怎样消费 output：nonstream 汇总成最终 response；stream 按输出产生 SSE chunk。

即使使用 stream，`core_output`、`async_output`、`http_chunk` 和客户端 first chunk 也不
必一一对应。output collector 可以聚合输出，serving 层还要处理 detokenization 后的文本、
finish reason 和 usage。解析器的 assertion 要求关键边界存在，但不武断要求计数相等。

客户端在 `client.json` 保存 first content chunk 的 monotonic timestamp。server trace
有同机 monotonic clock，可以对照顺序；这个间隔包含多层处理和传输，不能直接称为
某个 Python 函数耗时。

## 8. Abort 路径为什么不能只看客户端关闭成功

`lifecycle_requests()` 的 abort 请求设置较大的 output 上限，在第一个非空 chunk 后
立即 `close()` SSE iterator。已有 HTTP helper 会关闭底层 response/socket。

随后 runner 等待 trace 满足：

```text
abort_sent → abort_received → request_freed(FINISHED_ABORTED)
```

若服务已经自然完成，释放原因是 `FINISHED_LENGTH_CAPPED`，parser 会报告“取消与正常
完成竞争，未证明 abort”。客户端提前断开并不自动等于资源提前释放；应调整短实验形状
重跑，保留失败 trace，不修改 assertion 来迁就结果。

validation failure 使用不存在的模型名，客户端要求返回 400/404/422。该错误在有效内部
request ID 创建前发生，因此单独记录 HTTP 错误证据，不凭空要求 Engine Core 事件。

## 9. Trace 的开关、格式和体积

`VLLM_STUDY_TRACE_DIR` 未设置时 `emit()` 直接返回。每条 JSONL 有 monotonic `ts_ns`、
UTC wall time、PID、thread、component、event、request ID，以及允许的计数/状态字段。
不记录 prompt、完整 token ID 数组、生成内容或 Authorization。

每个 PID 写一个文件，线程内用锁保持整行写入，避免多进程输出互相穿插。超过配置的
event limit 时写 `trace_truncated`，parser 会拒绝把它当完整 trace。
Week 5/6 的 server runner 会清除继承来的 trace 环境变量，避免调试开关污染性能实验。

## 10. 在 VM 上准备并运行

Mac 可直接预演，缺少 Week 6 结果时输出的是待替换的 baseline 命令：

```bash
make plan-week07 PYTHON=python3.12
```

正式运行需要 Week 6 已导出的 operating point 或经过验证的 fallback。准备源码时：

```bash
git clone --branch v0.10.2 --depth 1 https://github.com/vllm-project/vllm.git vendor/vllm
git -C vendor/vllm rev-parse HEAD
git -C vendor/vllm apply --check ../../patches/week07-request-trace.patch
git -C vendor/vllm apply ../../patches/week07-request-trace.patch
```

这些源码准备命令在 VM 执行；本仓库不自动安装或更改环境依赖。按固定版本的开发安装
说明把这个 checkout 接入专用 vLLM 环境，并复核 package path。该版本允许在已有匹配
wheel 的基础上用预编译组件进行 editable 安装，具体 CUDA/wheel 匹配需在 VM 核对；
仅把 checkout 加入 `PYTHONPATH` 可能缺少编译扩展，不能据此认为环境已经可运行。

```bash
make run-week07 VLLM_PYTHON=.venv-vllm/bin/python
```

产物位于 `results/week07/traces/<session-id>/`：source/runtime、server log、client、
按 PID 分开的 JSONL，以及 `nonstream/`、`stream/`、`abort/` 下的 events 和 Mermaid 输入。
源码检查或路径断言失败会保留已有 trace 和日志。完成后同步结果并停止 VM。

## 11. 自测与下一周

[trace 测试](../tests/test_scheduler_study.py) 检查默认关闭、字段保护、截断拒绝、PID
边界和真正的 aborted free。patch 的应用和语法检查验证源码改动形状；它们不能替代
L4 上的实际 nonstream、stream、abort 证据。

Week 8 从 `request_queued` 继续追踪：进入 waiting 后，何时 admit，分配多少 token，
什么时候需要重新排队。API 参数与 scheduler decision 的联系从这里开始建立。
