# InferencePool / EPP 实验合同

本合同对应 [Week 17 代码导读](week-17-code-walkthrough.md) 与
[配置](../configs/week17-inferencepool.yaml)，实际验收状态见 [报告](../reports/week17.md)。

| 边界 | 实现/证据要求 |
|---|---|
| Core Gateway API | 保留固定 GatewayClass/controller/data plane 与 core v1 对象 |
| GAIE | `v1.0.0` CRD；`InferencePool` 使用 `inference.networking.k8s.io/v1` |
| 模型候选 | selector `app=vllm`，每 endpoint 为完整单节点 G-GPU replica |
| 端口 | 模型 identity 8081、engine metrics 8000、EPP gRPC 9002；以冻结配置为准 |
| Failure mode | explicit FailClose、explicit FailOpen、omitted default FailClose 分开 |
| 版本 | CRD/artifact SHA-256、EPP source commit/image digest 与兼容说明 |
| 日志 | request ID、phase、attempt、selected/actual Pod UID、status、timestamp |

`decisions.jsonl` 每行至少含 `request_id`、`selected_pod_uid`、`ready_candidate_uids`；
可加 `phase`、`routing_latency_ms`、`load_age_seconds`、fallback reason。
`dispatches.jsonl` 每行至少含 `request_id`、`pod_uid`，另存 attempt、route、HTTP status。
两个文件分别来自 EPP 和 gateway/identity 归一化后的同一 attempt 层，不能将两跳重复计数。

Pool membership/status、Gateway parent conditions、EPP endpoint readiness 与 stats 保存为
对象/metrics 快照。Selection 不等于 dispatch；HTTP 错误不等于从未 dispatch；流式连接
开始后不自动迁移已发 tokens。取消/timeout/retry 各自保留结果和次数。

Reference EPP 仅用于规范学习和 conformance。官方 Job 的 test profile、版本、命令、
pass/fail/skip 与原始输出单独保存；不把该测试扩写成吞吐、HA 或生产保证。

Week 18 更换 EPP 时保持本合同的数据面与证据格式，使用同一份 APC-on baseline。
