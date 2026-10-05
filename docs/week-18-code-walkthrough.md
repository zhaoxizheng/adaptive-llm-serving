# Week 18 中文代码导读：llm-d Load-aware / Precise Prefix-aware Routing

本周把 reference EPP 换成固定 release 的 llm-d EPP，保持 Gateway、InferencePool、APC-on
模型与双副本 shape 一致。对应 [学习计划](week-18-plan.md)、[参考资料](week-18-references.md)、
[运行手册](week-17-20-runbook.md)、[routing contract](llm-d-routing-contract.md)。

## 1. 代码地图

| 文件 / 函数 | 作用 |
|---|---|
| [week18-routing.yaml](../configs/week18-routing.yaml) | 两条 pipeline、四类 workload、两档负载与故障 |
| [platform_workload.py](../src/platform_workload.py) `matrix` | 交错 treatment，保留 paired repeat seed |
| 同文件 `make_jobs` | 生成 synthetic token IDs、prefix family 和 open-loop arrival |
| 同文件 `run_requests/safe_row` | 有界并发、流式验证、最小证据保存 |
| [run_platform_study.py](../scripts/run_platform_study.py) `warmup/run_cell` | 重建 cache、逐 Pod 预热、采样和运行 |
| [analyze_platform.py](../src/analyze_platform.py) `prefix_analysis/grouped_slo` | identity 检查、实际 reuse 与分组 SLO |
| [llm-d-router/README.md](../deploy/llm-d-router/README.md) | release-specific 配置接入 |

## 2. 为什么保存两份实际 pipeline

`llmd_load` 与 `llmd_prefix` artifacts 保存两套实际 rendered resources；`load_pipeline`、
`prefix_pipeline` 保存插件顺序、配置与源码出处。运行器不自己实现一个简化 hash router
再把它命名为 llm-d precise-prefix。

需要控制 candidate gate、tokenizer、timeout、failure handling 等非实验配置。
如果两份 pipeline 在多个地方不同，报告结论就是完整 pipeline A/B，不能归因某个插件。
`cache_identity` artifact 记录固定实现如何连接 model/tokenizer/template/adapter 与 engine
cache identity；模型内容和完整 cache key 不作为实验日志保存。
`verify_live_case()` 在运行前把实际对象与所选 artifact 做字段子集比较，允许 API defaulting
补充字段，但拒绝把 load-aware 实际配置标成 precise-prefix 的实验结果。

## 3. `make_jobs()` 如何构造可比较流量

arrival RNG 与 prefix-family RNG 分开，因此 locality 改变不会顺带改变请求到达轨迹。
每个 repeat 改变 arrival/suffix seed，同一 repeat 的两个 pipeline 得到相同 trace。
`trace_metadata()` 保存整个合成输入的 SHA-256，不把 token IDs 写进结果文件。

| Workload | 构造方式 | 用途 |
|---|---|---|
| shared_prefix | family 均匀轮转，同 family 的前 512 tokens 相同 | 观察 locality |
| hot_prefix | 80% 概率选择 family 0，其余请求轮转 | 观察热点排队 |
| low_sharing | 和 shared 相同长度，每请求首 token 不同 | 观察无共享时的开销 |
| mixed | 每四个请求一个 long，其余 short | 观察短请求尾延迟 |

独立首 token 能避免 low-sharing 对照意外共享前缀块；shared prefix 后仍有不同 suffix，
不会把整个请求变成相同缓存命中。这里的 family ID 是实验分组，不是 engine cache key。

`matrix()` 输出 96 个正式 cells：3 repeats × 4 workloads × 2 rates × 2 cache states ×
2 pipelines。repeat 0 先 load-aware，repeat 1 先 precise-prefix；下一次再轮转。
故障 case 不在正式矩阵里。

## 4. Cold / warm 的实际代码路径

`warmup()` 显式 restart Deployment，等待当前 generation、Ready 和 GPU/TP/image/args
匹配。随后对每个 replica 做 model warmup；这些请求使用独立 leading ID，不会预热正式
请求的 shared prefix。Warm cell 再逐 Pod 预热四个正式 family，每次只生成一个 token。

这种预热保证两个 pipeline 的起点一致，并记录预热成本；它没有证明 EPP index 已同步。
从 EPP 的实际事件确认 cache index、process generation 与 residency，再解释 warm 结果。
Cold cell 用于观察自然 locality 建立；不能把 warm cell 更低 TTFT 当成路由算法独有收益。

## 5. `run_requests()` 为什么不用无限排队

它按预生成 offset 开环发请求，以 semaphore 限制正在执行的任务。达到 `max_inflight`
时记 `client_overload`，不把请求偷偷排进无限 executor 队列。客户端到达延迟也记入 TTFT。
如果 arrival lag 超过 baseline 阈值或发生 client overload，summary 标记
`performance_eligible: false`。

SSE 校验复用前序的 `experiment_client.request`：必须完成 stream、输出长度与 usage
符合请求、finish reason 为 length，才算 success。生成文本仅在内存中处理；`safe_row()`
用允许字段列表落盘，因此不会保存 output excerpt、prompt IDs 或 cache key。
这证明流式协议/计数正确；语义输出一致性仍需额外的固定输入输出验证证据。

## 6. Precise prediction 不等于实际 reuse

`prefix_analysis()` 用 request ID 连接 decision 和 engine reuse，并要求双方完整 identity：

```json
{"model_revision":"commit","tokenizer_revision":"commit","template_revision":"sha256","adapter_id":"none","namespace":"serving-lab","pod_uid":"pod-a","process_generation":"boot-1"}
```

Decision 外层包含 `selected_pod_uid`、`predicted_tokens`；engine 外层包含
`reused_tokens`。即便 predicted=512，缺失 engine 记录仍输出 `not_observed` 和 null。
实际 reused=0 是合法观测，不会被当成缺失。Pod UID 或 process generation 不同则为
`identity_mismatch`；同一个 request 有多条 engine 记录为 `ambiguous_attempts`。

Pod replacement 后不能只按 Pod name 复用状态；同名/重建资源的 UID 和进程代次不同。
缺失指标、过期 load、index residency 与 engine eviction 都要按所选实现记录，代码不会
从 route affinity 推断它们。

## 7. 命令与结果解读

```bash
bash scripts/run_week18_llmd_routing.sh --plan
bash scripts/run_week18_llmd_routing.sh --apply --case load_aware
bash scripts/run_week18_llmd_routing.sh --run --case load_aware \
  --workload hot_prefix --rate low --cache cold --repeat 0
bash scripts/run_week18_llmd_routing.sh --apply --case precise_prefix
bash scripts/run_week18_llmd_routing.sh --run --case precise_prefix \
  --workload hot_prefix --rate low --cache cold --repeat 0
python3.12 -m src.analyze_platform prefix \
  --input results/week18/SESSION/decisions.jsonl \
  --aux results/week18/SESSION/engine-reuse.jsonl \
  --output results/week18/SESSION/prefix.json
python3.12 -m src.analyze_platform slo \
  --input results/week18/SESSION/clients.jsonl \
  --output results/week18/SESSION/slo.json
```

`grouped_slo()` 用相同观测窗口给出 overall、short/long 和 family 分组的 SLO/goodput，
失败、超时和 overload 均保留在分母。样本不足 1000 时 P99 只是探索值；默认 120 请求
是可跑通的候选规模，正式采样量应在冻结配置时根据预算确定。

先确认 load generator、Gateway、EPP 没有饱和，再解释 endpoint 差异。更高 actual reuse
若伴随更高 queue/P99 或更差 SLO，应该作为反例保留，而不是只展示命中率。

Stale/missing load、cache churn 和 EPP timeout 需提供各自 apply/restore artifacts；
`--fault --case pod_replacement` 内建删除一个 lab Pod 的流程。故障后切回 load-aware，
执行完整 smoke，并填写 [week18 报告](../reports/week18.md)。

## 8. 测试怎样支撑代码说明

[workload 测试](../tests/test_platform_runner.py) 验证到达轨迹相同、共享前缀与唯一 suffix、
热点分布、96-cell 交错矩阵和文本不落盘。[分析测试](../tests/test_platform_analysis.py)
验证 missing engine evidence、实际 zero reuse 和进程代次错配。它们不替代真实 llm-d
plugin、缓存状态或 GPU 实验。
