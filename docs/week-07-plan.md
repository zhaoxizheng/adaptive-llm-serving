# Week 7 Plan: 追踪 vLLM API 到 Engine Core 的请求链路

> 时间预算：10–12 小时
>
> 本周主线：从“会使用 vLLM”切换到“能解释 vLLM”。固定一个源码 revision，围绕一条最小 non-streaming 请求和一条 streaming 请求，自顶向下追踪 OpenAI API、请求校验、tokenization、AsyncLLM、Engine Core、进程间通信和输出返回。

## 本周目标

完成本周后，应当能够：

1. 画出 vLLM V1 单实例中 API server、Engine Core 和 worker 的进程/组件边界。
2. 从 OpenAI-compatible endpoint 追踪一个 request ID 到 engine request。
3. 解释 request validation、chat template、tokenization 和 sampling params 在哪里发生。
4. 解释 AsyncLLM 与 Engine Core 如何通信，以及请求与输出如何跨边界传递。
5. 区分 streaming response、non-streaming response 和 abort/cancel 路径。
6. 通过最小 trace 证明调用链，而不是只画一张从文档抄来的架构图。

## 本周边界

- 固定 Week 6 验证过的 vLLM release/tag 或 commit，源码链接在笔记中转换为该 revision 的 permalink。
- 主线只追踪 V1 engine；如果当前版本回退到 legacy/V0，先记录原因，不同时阅读两套实现。
- 使用最小模型和 1–2 个请求做控制流实验，不运行完整性能矩阵。
- 可以添加本地 debug log、trace hook 或测试，但不在本周改变 scheduler 行为。
- 不深入 attention kernel、CUDA graph、distributed executor 或 KV block 算法；这些分别留到后续周次。

## 本周最终产出

- `vendor/vllm-revision.txt`：阅读用 tag/commit 与安装包版本
- `docs/vllm-source-map.md`：组件、关键类、关键方法和 permalink
- `docs/vllm-request-lifecycle.md`：请求与输出时序图
- `patches/week07-request-trace.patch`：可重复应用的最小 trace 改动
- `scripts/run_week07_trace.sh`：启动 server、发送请求并收集 trace
- `results/week07/traces/`：non-streaming、streaming 和 abort trace
- `reports/week07.md`：回答本周六个核心问题

## 版本冻结

在阅读前记录：

```bash
python -c 'import inspect, vllm; print(vllm.__version__); print(inspect.getfile(vllm))'
python -m pip freeze > results/week07/pip-freeze.txt
git -C /path/to/vllm rev-parse HEAD
```

若 wheel 与源码 checkout 不一致，明确说明：

- 运行证据来自哪个安装版本
- 阅读和 patch 来自哪个 commit
- 两者是否由同一 release/tag 对齐

本周笔记中的源码引用必须包含 commit permalink；`main` 链接只用于导航。

## 要追踪的主链路

文件名会随版本变化，以下是问题顺序，不是必须硬编码的目录清单：

```text
HTTP endpoint
  → request validation / model lookup / chat template
  → tokenization and sampling parameters
  → AsyncLLM request submission
  → Engine Core client / IPC boundary
  → Engine Core request state
  → scheduler admission (Week 8 深入)
  → worker/model execution (Week 9 深入)
  → engine output
  → streaming chunk or final response
```

每经过一层，记录：

| 字段 | 要回答的问题 |
|---|---|
| component | 属于哪个进程或线程？ |
| input type | 接收的对象和关键字段是什么？ |
| request ID | 是否保留、转换或新建？ |
| blocking | await、queue、IPC 还是同步调用？ |
| output type | 向下一层或客户端返回什么？ |
| failure | validation、disconnect 或 engine error 如何传播？ |

## Trace Contract

最小 trace 采用单行 JSONL，避免解析自由文本：

```json
{"ts_ns": 0, "pid": 0, "component": "api", "event": "request_received", "request_id": "..."}
```

建议字段：

- `ts_ns`：单机 monotonic timestamp，用于事件排序
- `wall_time_utc`：便于与 server log 对齐
- `pid` / `thread`：证明进程边界
- `component` / `event`
- `request_id`
- `prompt_tokens` / `max_tokens`
- `finish_reason` 或 `error_type`

禁止在 trace 中记录原始用户 prompt、Authorization header、完整生成内容或其他敏感数据。

## 三条实验路径

### Non-streaming

- 一个固定 prompt
- greedy decoding
- 4 个 output tokens
- 记录接收、入 engine、首个 engine output、完成 response

### Streaming

- 使用相同 prompt 和 decoding 配置
- 记录每次 engine output 与 HTTP chunk 是否一一对应
- 区分 first engine output 和 first client-visible chunk

### Abort / disconnect

- 客户端在首个 chunk 后主动断开
- 追踪 cancel/abort 如何进入 engine
- 验证 request state 最终释放，而不是继续生成到 `max_tokens`

## 每日安排

### Day 1：冻结 revision 与架构地图（约 1.5 小时）

- [ ] clone 或定位与 Week 6 安装版本对应的 vLLM source
- [ ] 保存 tag/commit 和 Python package path
- [ ] 阅读 Architecture Overview 和仓库 developer docs
- [ ] 标记 API server、AsyncLLM、Engine Core、worker 的入口文件
- [ ] 画出初版进程图，并把未知边界标为问题

### Day 2：HTTP 与 request preprocessing（约 1.5–2 小时）

- [ ] 从 `/v1/completions` 或 `/v1/chat/completions` endpoint 开始
- [ ] 找到 model lookup、request validation 和 error response
- [ ] 追踪 chat template 与 tokenization 的位置
- [ ] 找到 sampling parameters 如何转换为 engine 输入
- [ ] 为关键函数建立 commit permalink

### Day 3：AsyncLLM 与 Engine Core 边界（约 2 小时）

- [ ] 找到请求进入 AsyncLLM 的方法
- [ ] 找到 request ID 与 output stream 的注册方式
- [ ] 识别 Engine Core client 与 IPC transport
- [ ] 记录输入、输出与 control message 的数据结构
- [ ] 用 PID 和 trace 证明哪些调用跨进程

### Day 4：输出与 streaming（约 1.5 小时）

- [ ] 追踪 Engine Core output 返回 AsyncLLM
- [ ] 找到 detokenization 和 response object 构造
- [ ] 比较 streaming 与 non-streaming 的共享和分叉路径
- [ ] 记录 finish reason、usage 和 final chunk 如何产生

### Day 5：最小 instrumentation（约 2 小时）

- [ ] 在 6–10 个关键边界添加结构化 trace event
- [ ] 保持 trace 默认关闭，并通过环境变量启用
- [ ] 分别运行 non-streaming 与 streaming 请求
- [ ] 生成按 request ID 排序的 sequence diagram 输入
- [ ] 确认 instrumentation 不打印 prompt 或凭证

### Day 6：Abort、错误和资源释放（约 1.5 小时）

- [ ] 主动断开 streaming client
- [ ] 发送一个 validation failure 请求
- [ ] 追踪 abort/cancel 和 error propagation
- [ ] 验证请求最终不在 running/waiting state 中
- [ ] 将预期事件写成最小测试或 trace assertion

### Day 7：整理 source map 与报告（约 1.5–2 小时）

- [ ] 完成组件图和两张 sequence diagram
- [ ] 每个箭头附关键函数与 permalink
- [ ] 区分源码确认、运行确认和仍未确认的推断
- [ ] 将 scheduler admission 的未解问题移交 Week 8
- [ ] 保存 patch，不直接维护长期 fork

## 报告必须回答的问题

1. HTTP 请求在哪一步获得内部 request ID？
2. Tokenization 位于 API process 还是 Engine Core process？
3. AsyncLLM 如何把一个请求与异步输出流关联起来？
4. API server 与 Engine Core 的进程和 IPC 边界是什么？
5. Streaming response 的 first chunk 比 first engine output 多经过哪些步骤？
6. Client disconnect 后，取消信号如何传播，资源何时释放？

## 本周不要做什么

- 不按仓库目录从头到尾顺序阅读。
- 不在笔记中只贴文件名，不记录 revision 和关键函数。
- 不将日志时间戳直接当作精确性能 profiler。
- 不为了“看见更多”在 decode hot path 每步打印大量文本。
- 不同时追踪多模型、multi-GPU、distributed serving。
- 不把从架构图推断的路径写成已经由 runtime trace 证实。

## 完成标准

- [ ] 阅读源码与运行 package 对齐到明确 revision
- [ ] API、AsyncLLM、Engine Core 和 worker 边界已画清
- [ ] Non-streaming 与 streaming trace 可由脚本复现
- [ ] 每个关键箭头都有源码 permalink 或 runtime event
- [ ] Abort/disconnect 路径已验证请求最终释放
- [ ] Trace 默认关闭且不包含 prompt、token 或凭证内容
- [ ] Week 8 的 scheduler 入口和未解问题已列出
- [ ] GCP VM 已停止，trace 已同步并绑定 Git commit
