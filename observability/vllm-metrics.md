# vLLM 0.10.2 观测契约

这份映射由固定 revision 的 V1 metrics 实现核对，是运行时 discovery 的候选清单。
尚未在当前机器运行 GPU server 或采集实际 `/metrics`；每次实验中的 `metrics-before.txt`
和 `prometheus.json.inventory` 才是该 run 的实际 exposition 证据。

配置：[queries.yaml](prometheus/queries.yaml)。采集与解释见
[Week 5 代码导读](../docs/week-05-code-walkthrough.md)。

| 含义 | 候选 metric | 类型 | 单位 / 使用方式 |
|---|---|---|---|
| 等待请求 | `vllm:num_requests_waiting` | gauge | requests；后半窗口增长斜率 |
| 运行请求 | `vllm:num_requests_running` | gauge | requests；与 scheduler active sequences 对照 |
| KV 占用 | `vllm:kv_cache_usage_perc` | gauge | fraction，0–1；不是 0–100 |
| 输入 token | `vllm:prompt_tokens_total` | counter | tokens；保存累计值后取差分 |
| 输出 token | `vllm:generation_tokens_total` | counter | tokens；保存累计值后取差分 |
| 成功完成 | `vllm:request_success_total` | counter | requests；包含完成原因 label，查询对其求和 |
| 请求排队 | `vllm:request_queue_time_seconds_bucket` | histogram bucket | seconds；`histogram_quantile` + bucket rate |
| prefix hits | `vllm:prefix_cache_hits_total` | counter，可选 | 命中的 tokens；本周 APC 关闭，仅背景观测 |

Counter 的 `_total` 后缀来自 Prometheus client exposition，源码构造名称可能不带它。
预期业务 labels 包括 `model_name`、`engine`；成功计数还有 `finished_reason`。实际 label
集合从 exposition 保存，不硬编码为验收依据。`job` / `instance` 是 Prometheus target
labels，并不是 vLLM 必须自己输出的 labels。

已有 Prometheus 应以配置的 `scrape_interval_seconds` 抓取该实例；`observability.labels`
必须准确选择这一台 VM 的 server。示例 `instance=127.0.0.1:8000` 仅适用于 Prometheus
同机抓取；远端已有 Prometheus 需要可达 target 与相符标签，不能把查询 selector 留成
本机示例。服务默认 loopback，远程访问按既有 SSH/私网方案设置。

Histogram 的 rate lookback 是四个 scrape interval，查询从 measurement start 加上这个
lookback 开始，避免将 warmup 混入。其他 gauge/counter 使用完整 measurement window。
保存 query expression、完整 API response、解析后的 samples 和错误原因，可离线重建。

缺少必需 metric、Prometheus warning、缺序列、非有限样本、query gap、counter reset、
stale scrape 都使该 run 无效。prefix hits 缺失只是可选背景信号；不能据此推断 APC
行为。当前没有假定存在统一的 vLLM failure counter，timeout、HTTP error、invalid
response 从 client records 计算，再与 server log 中错误核对。

GPU CSV 的 `memory_used_mib` 是设备 framebuffer 总占用，不能直接视为模型权重显存。
`utilization_pct` / `power_w` 是采样时的设备指标，不是每请求执行阶段 profiler。

源码依据：[V1 PrometheusStatLogger](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/v1/metrics/loggers.py)。
