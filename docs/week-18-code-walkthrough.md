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

## 3. 核心代码精读

### Load 与 prefix 评分怎样进入同一个选择结果

本节只固定**源码阅读样本**：llm-d-router `v0.11.0 / a5cbe600ebade00cf3e9885beaf2bfacddeabce1`。
该版本依赖 GAIE `v1.5.0`，不表示它能直接部署到 Week 17 的 v1.0.0 环境；实验仍需
独立完成兼容性和 artifacts 锁定。这里只借其完整实现解释 routing 机制。

`SchedulerProfile.Run()` 顺序执行 filters → scorers → picker；加权核心在：

源码：[pkg/epp/scheduling/scheduler_profile.go](https://github.com/llm-d/llm-d-router/blob/a5cbe600ebade00cf3e9885beaf2bfacddeabce1/pkg/epp/scheduling/scheduler_profile.go#L240-L256)，第 240–256 行；以下为原文摘录，仅移除公共缩进。

```go
// Iterate through each scorer in the chain and accumulate the weighted scores.
for _, scorer := range p.scorers {
	typedName := scorer.TypedName()
	if verboseEnabled {
		verbose.Info("Running scorer plugin", "plugin", typedName)
	}
	scores := runScorer(ctx, tracer, tracingActive, scorer, request, endpoints)
	for endpoint, score := range scores { // weight is relative to the sum of weights
		if debugEnabled {
			debug.Info("Calculated score", "plugin", typedName, "endpoint", endpoint.GetMetadata().ID, "score", score)
		}
		weightedScorePerEndpoint[endpoint] += enforceScoreRange(score) * scorer.Weight()
	}
	if debugEnabled {
		debug.Info("Completed running scorer plugin successfully", "plugin", typedName)
	}
}
```

每个 scorer 对相同的过滤后候选返回分数；框架约束评分范围，再乘该插件权重累加。
最终按配置的 picker 决策，所以“prefix-aware”不等于永远选择命中最多的 endpoint。
例如教学权重 load=2、prefix=1，A 的两分为0.2/1.0，总分1.4；B 为0.9/0.0，总分1.8。
若 picker 取最高分，B 胜出。这里数字用于手算，不是推荐阈值或默认配置。

### Precise prefix match 为什么不能累计离散命中 blocks

阅读 `prefixAccumulator.endKey()` 中继续一条 prefix chain 的判断。下面只摘录循环
前半；完整函数随后更新 confirmed/tier 状态，把仍有效的项加入 `keep`，再更新 active：

源码：[pkg/kvcache/prefix_match.go](https://github.com/llm-d/llm-d-router/blob/a5cbe600ebade00cf3e9885beaf2bfacddeabce1/pkg/kvcache/prefix_match.go#L423-L430)，第 423–430 行；以下为原文摘录，仅移除公共缩进。

```go
keep := a.active[:0]
for _, i := range a.active {
	s := &a.slots[i]
	if s.seen != a.keyStamp {
		continue // the chain ends at the first key the pod does not hold
	}
	s.matched++
	s.score += s.weight
```

输入按请求的 block keys 顺序遍历。`seen == keyStamp` 表示这个 Pod 在当前 key 上
仍有记录；没看到就不加入后面的 `keep`，其 chain 终止。后续 key 再次出现这个 Pod，
也不能填补已经断掉的前缀。`matched` 是连续长度；`score` 还可按存储 tier 的权重累计。

手算 keys=`[h0,h1,h2]`：A 持有 h0/h2，B 持有 h0/h1。A 的连续命中只有1个 block，
B 为2个；不能把 A 算成2。第一次 key 决定初始候选，后续只能延长尚未断开的 chain。
同 Pod 同 key 的多个 rank/tier 记录会按实现折叠，不直接当成多个可复用 blocks。

**设计取舍与边界。** Indexer 中的精确信息也有事件延迟和 eviction 竞态；该阅读版本
还区分 speculative 与 confirmed chain。因此预测是路由决策输入，engine 实际 reuse
仍要按 request ID、Pod UID、process generation 和模型/tokenizer identity 独立关联。
负载过期、热点 prefix 集中和 Pod replacement 都可能让静态高分失去意义。

**读后自检。** 为什么“路由到同一个 Pod”不足以证明复用了 KV？如果 index 命中512
tokens、engine 实际0，应该保存怎样的两份证据，而不是直接改写成一次成功命中？

## 4. `make_jobs()` 如何构造可比较流量

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

## 5. Cold / warm 的实际代码路径

`warmup()` 显式 restart Deployment，等待当前 generation、Ready 和 GPU/TP/image/args
匹配。随后对每个 replica 做 model warmup；这些请求使用独立 leading ID，不会预热正式
请求的 shared prefix。Warm cell 再逐 Pod 预热四个正式 family，每次只生成一个 token。

这种预热保证两个 pipeline 的起点一致，并记录预热成本；它没有证明 EPP index 已同步。
从 EPP 的实际事件确认 cache index、process generation 与 residency，再解释 warm 结果。
Cold cell 用于观察自然 locality 建立；不能把 warm cell 更低 TTFT 当成路由算法独有收益。

## 6. `run_requests()` 为什么不用无限排队

它按预生成 offset 开环发请求，以 semaphore 限制正在执行的任务。达到 `max_inflight`
时记 `client_overload`，不把请求偷偷排进无限 executor 队列。客户端到达延迟也记入 TTFT。
如果 arrival lag 超过 baseline 阈值或发生 client overload，summary 标记
`performance_eligible: false`。

SSE 校验复用前序的 `experiment_client.request`：必须完成 stream、输出长度与 usage
符合请求、finish reason 为 length，才算 success。生成文本仅在内存中处理；`safe_row()`
用允许字段列表落盘，因此不会保存 output excerpt、prompt IDs 或 cache key。
这证明流式协议/计数正确；语义输出一致性仍需额外的固定输入输出验证证据。

## 7. Precise prediction 不等于实际 reuse

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

## 8. 命令与结果解读

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

## 9. 测试怎样支撑代码说明

[workload 测试](../tests/test_platform_runner.py) 验证到达轨迹相同、共享前缀与唯一 suffix、
热点分布、96-cell 交错矩阵和文本不落盘。[分析测试](../tests/test_platform_analysis.py)
验证 missing engine evidence、实际 zero reuse 和进程代次错配。它们不替代真实 llm-d
plugin、缓存状态或 GPU 实验。
