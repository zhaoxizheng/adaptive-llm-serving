# Week 7 Reference Reading

第七周围绕一条具体请求阅读源码：HTTP 请求如何变成 engine request、跨越哪些进程边界、输出如何回到 streaming client。先建立 source map，再通过 trace 验证。

以下链接已于 2026-08-31 使用浏览器在线核对。GitHub 的 `main` 链接仅用于入口发现；正式笔记必须替换为本周固定 commit 的 permalink。

## 必读：架构与贡献者文档

1. [vLLM Architecture Overview](https://docs.vllm.ai/en/latest/design/arch_overview/)
   - 建立 API server、Engine Core、worker 和 model runner 的组件地图。
   - 阅读时标记哪些是进程、哪些是对象、哪些是逻辑模块。

2. [vLLM GitHub Repository](https://github.com/vllm-project/vllm)
   - 固定与运行版本对应的 tag 或 commit。
   - 优先使用仓库内 developer docs、tests 和 examples 解释当前实现。

3. [vLLM Contributing Guide](https://docs.vllm.ai/en/latest/contributing/)
   - 了解开发环境、测试入口和代码规范。
   - 本周 patch 保持最小、可关闭，并适合后续转为测试或文档贡献。

## 源码入口：请求进入 Engine

4. [OpenAI API Server Source](https://github.com/vllm-project/vllm/blob/main/vllm/entrypoints/openai/api_server.py)
   - 从应用构造和 endpoint 注册开始定位实际 serving handler。
   - 不在这个文件停止；继续追踪 serving layer 和 engine client。

5. [AsyncLLM Source](https://github.com/vllm-project/vllm/blob/main/vllm/v1/engine/async_llm.py)
   - 关注 add/generate、output handler、request stream 和 abort。
   - 记录 request ID 在哪些结构中作为 key。

6. [Engine Core Source](https://github.com/vllm-project/vllm/blob/main/vllm/v1/engine/core.py)
   - 找到 request/control message 进入 Engine Core 的边界。
   - 本周只追踪 admission 和 output，不深入 scheduler 算法。

## 必读：协议与异步机制

7. [OpenAI API Reference: Chat](https://developers.openai.com/api/reference/resources/chat)
   - 理解 vLLM 兼容层对外承诺的 request、streaming 和 usage 语义。
   - 这里只用于协议对照，不假定 vLLM 内部实现与 OpenAI 相同。

8. [Python `asyncio` Task](https://docs.python.org/3/library/asyncio-task.html)
   - 复习 task、cancellation、timeout 和 async generator 行为。
   - 用于理解 client disconnect 后的取消传播。

9. [PyZMQ `zmq.asyncio`](https://pyzmq.readthedocs.io/en/latest/api/zmq.asyncio.html)
    - 理解 asyncio socket、send/recv future 和 poll。
    - 结合固定 revision 确认 vLLM 当前实际 transport，不从 roadmap 图反推实现。

## 调试辅助

10. [Python `inspect`](https://docs.python.org/3/library/inspect.html)
    - 用 `inspect.getfile()` 确认运行时 import 的 package 文件。
    - 避免修改 checkout A，却运行环境中的 wheel B。

11. [Mermaid Sequence Diagram](https://mermaid.js.org/syntax/sequenceDiagram.html)
    - 将 trace 事件转成可复核的请求时序图。
    - 图中保留进程边界和 await/IPC，不只画函数调用。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 1–3、10 | revision 与组件地图 |
| Day 2 | 4、7 | HTTP、校验和协议 |
| Day 3 | 5–6、9 | AsyncLLM 与 Engine Core |
| Day 4–6 | 8、11，回看源码 | streaming、abort 和 trace |
| Day 7 | 固定 commit permalinks | source map 与报告 |

## 阅读后的自测问题

1. API server、AsyncLLM、Engine Core 和 worker 是否处于同一进程？如何证明？
2. Chat template 与 tokenization 在请求链路的什么位置？
3. 内部 request ID 在哪里产生，又如何关联 output stream？
4. Streaming 和 non-streaming 在哪一层开始分叉？
5. Engine output 到 client-visible chunk 之间还有哪些工作？
6. Client disconnect 如何变成 engine abort？
7. 为什么源码笔记必须绑定 commit permalink，而不能只链接 `main`？
