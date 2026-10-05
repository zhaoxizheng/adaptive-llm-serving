# HPA / KEDA 扩缩容实验合同

代码解释与命令见 [Week 20 中文导读](week-20-code-walkthrough.md)。默认 target 为
`apps/v1 Deployment/vllm`；每个 Pod 的 GPU 数 G 和 TP=G 固定，缩放只改变 replicas。

| 模式 | 唯一控制路径 | replicas 范围 |
|---|---|---|
| fixed_1 / fixed_2 | 人工有界 `/scale`，无动态 writer | 1 / 2 |
| hpa | Adapter → custom.metrics API → independent HPA | 1–2 |
| keda | KEDA scaler/metrics server → KEDA-owned HPA | 1–2 |

不得存在 KServe parent、独立 HPA 与 ScaledObject 并行、持续 GitOps/manual replicas
覆盖。切换保存前后 writer/managedFields，并等待旧对象消失。

默认 metric 候选为 Ready vLLM Pods 的 waiting requests 总和，经 freshness 与完整采样
检查生成 `serving_lab:queue_total`。HPA 使用 custom `Object`、KEDA 使用 `External`，两条路径均 `AverageValue`，threshold 单位 requests
per replica。校准完成前禁止正式运行；scrape、poll、HPA sync、query 与窗口分别保存。

Prometheus recording rule 的标签依赖固定 scrape mapping：namespace、workload、pod、
model_name；运行时另以 Pod UID 区分 replacement。不要把重复 scrape/历史 Pod/其他模型
合入总量，也不要把 missing/stale/NaN/Inf 当成 zero。API response 与 HPA currentMetrics
必须能对账；名称相同不是语义相同的证明。

Timeline 保存 timestamp、实际 Deployment/HPA/Pod 对象与 allocated_gpus。
Cold-start 归一化文件每行以 pod_uid 关联 observed、desired、scheduled、model_ready、
route_eligible、first_token 六个 epoch 秒字段；缺失阶段不插值伪造。

`allocated GPU-hours = integral(actual allocated GPU count) / 3600`。
`billed GPU/node-hours` 必须来自同一时间边界的 provider billing 或 node lifecycle。
没有 billing 时为 null。Warmup、idle、load generator 窗口与实际资源计费范围分别说明。

分开注入 Prometheus、adapter、KEDA scaler 与 metrics-server 故障。长 SSE drain 要有
实际 termination overlap；cooldown 不替代 termination grace。默认不启用 zero/idle，
也不根据 Pod 数减少声称云账单下降。
