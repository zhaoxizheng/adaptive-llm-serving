# vLLM V1 Source Map：请求链路

阅读基线为 `v0.10.2`，commit `01efc7ef781391e744ed08c3292817a773d654e6`。
下表是源码确认；运行时确认要引用自己的 trace session、PID 和 event，不由文件存在
自动推导。应用方式见 [Week 7 导读](week-07-code-walkthrough.md)。

| 组件 | 关键入口与固定源码 | 输入 / 输出 | 边界与本周观察 |
|---|---|---|---|
| OpenAI HTTP app | [api_server.py](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/entrypoints/openai/api_server.py) | HTTP / Pydantic request → serving handler | API 进程；HTTP/schema 错误可能发生在内部 ID 创建前 |
| Completion serving | [OpenAIServingCompletion.create_completion](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/entrypoints/openai/serving_completion.py) | model 校验、prompt rendering、SamplingParams → engine generator | 生成 `cmpl-...` 与每 prompt 的 `...-0` ID；trace 的 `request_received` 在前置检查后 |
| Chat serving | [OpenAIServingChat.create_chat_completion](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/entrypoints/openai/serving_chat.py) | messages / chat template → engine prompt | 本周源码阅读；默认 trace runner 走 completions，因此没有声称 chat path 已运行 |
| 共享 serving 工具 | [serving_engine.py](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/entrypoints/openai/serving_engine.py) | model lookup、request ID、错误与预处理辅助 | `_base_request_id` 优先读取 `X-Request-Id` |
| AsyncLLM | [add_request / _add_request / generate / abort](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/v1/engine/async_llm.py) | prompt / SamplingParams → RequestOutputCollector → RequestOutput | 运行于 API 所在进程；注册 output processor 后交给 Engine Core client |
| 输入 Processor | [Processor.process_inputs](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/v1/engine/processor.py) | prompt → prompt string 与 EngineCoreRequest | 包含 tokenization / input processing；并非把原始 HTTP request 送到 GPU |
| IPC client | [AsyncMPClient.add_request_async](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/v1/engine/core_client.py) | EngineCoreRequest / 控制消息 ↔ EngineCoreOutputs | 单实例异步模式使用 ZMQ；具体 transport 与序列化见该 revision |
| Engine Core | [EngineCore.add_request / step / abort_requests](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/v1/engine/core.py) | Request → scheduler decision → ModelRunnerOutput → EngineCoreOutputs | 与 API 进程的 PID 差异由 trace 验证 |
| OutputProcessor | [process_outputs](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/v1/engine/output_processor.py) | token IDs / finish → detokenized output、异步 collector | output_handler 收到 Engine Core output 后调用，generate 从 collector 取回 |
| GPU worker / runner | [GPUModelRunner](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/v1/worker/gpu_model_runner.py) | SchedulerOutput → GPU 执行结果 | Week 7 只定位调用；本 patch 不测量 kernel 或证明 worker 独立 PID |

Tokenization 不必每次只发生在一个同名方法：serving renderer 可以已提供 token IDs，
Processor 也支持输入处理。共同边界是这些属于 API/AsyncLLM 侧预处理，Engine Core
消费转换后的请求；具体执行了哪条分支要结合请求类型阅读。

Week 8 的接续入口为 `EngineCore.step → Scheduler.schedule → update_from_output`，见
[scheduler map](vllm-scheduler-map.md)。Week 9 再深入 KV cache manager，保持本周关注点
在对象传递、等待/IPC、输出关联和取消传播。
