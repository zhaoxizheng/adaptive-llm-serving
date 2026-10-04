# Week 7：请求生命周期报告

状态：**固定源码已可阅读，runtime 请求 trace 尚未执行。**
源码与代码解释见 [Week 7 导读](../docs/week-07-code-walkthrough.md)。

## 实验身份与证据

- 阅读 commit：`01efc7ef781391e744ed08c3292817a773d654e6`
- 实际 package version / path、patch identity：待运行记录
- Git commit、GPU/runtime、Week 6 operating point：待填写
- nonstream / stream / abort 的 session、request ID、API / Core PID：待填写
- validation failure HTTP 证据：待填写

## 六个核心问题

1. HTTP 请求在哪里获得内部 request ID，parent 与 engine ID 如何关联？
2. Tokenization 位于哪个进程，当前请求实际经过哪条 preprocessing 分支？
3. AsyncLLM 如何注册并找到这个请求的异步 output collector？
4. 哪些调用跨越 API / Engine Core 的进程与 IPC 边界？
5. first core output 到 first client content chunk 还经过哪些步骤？
6. disconnect 后取消如何传播，哪条事件证明资源实际释放？

每个回答标记“源码确认 / runtime 确认 / 尚未确认”，并附 permalink 或 trace event。
自然完成与 abort 竞争的 run 必须保留并解释，不能算作取消验证成功。

## 完成与交接

- [ ] 三条路径可重跑，事件断言通过
- [ ] trace 不含 prompt、完整 token IDs、输出或凭证
- [ ] 时序图与实际 PID、event 顺序一致
- [ ] Week 8 scheduler admission 问题已列出
- [ ] trace 已同步，VM 与计费资源已核对
