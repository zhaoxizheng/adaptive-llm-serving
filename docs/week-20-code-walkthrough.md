# Week 20 中文代码导读：HPA/KEDA 扩缩容与可观测性

本周在 Week 18 的普通 `Deployment/vllm` 上比较 Fixed-1、Fixed-2、独立 HPA 和 KEDA。
对应 [学习计划](week-20-plan.md)、[参考资料](week-20-references.md)、
[运行手册](week-17-20-runbook.md)、[autoscaling contract](autoscaling-contract.md)。

## 1. 代码地图

| 文件 / 函数 | 作用 |
|---|---|
| [week20-autoscaling.yaml](../configs/week20-autoscaling.yaml) | query、阈值、burst/ramp 与分层 outage |
| [platform_contract.py](../src/platform_contract.py) `scaling_resources/writer_check` | 两种控制路径的资源与唯一 writer |
| [run_platform_study.py](../scripts/run_platform_study.py) `switch_scaler` | 删除旧 lab controller 后切换控制路径 |
| 同文件 `metric_sample/sample_loop` | Prometheus、adapter API、replicas、Pod allocation 时间线 |
| [platform_workload.py](../src/platform_workload.py) `make_jobs` | burst、ramp、短 burst、long-request mix |
| [analyze_platform.py](../src/analyze_platform.py) `metric_value/allocation_cost/cold_start` | 指标边界、积分与启动阶段 |
| [autoscaling/README.md](../deploy/autoscaling/README.md) | adapter、Prometheus rule、overlays |
| [dashboard](../dashboards/week20-autoscaling.json) | 同时间轴观察 metric/replicas/SLO |
| [serve_platform_metrics.py](../scripts/serve_platform_metrics.py) | 从运行中的 session 提供本地 Prometheus client metrics |

## 2. 同一个 target，不同的写入链

```text
Fixed: manifest/manual scale -> Deployment /scale
HPA: Prometheus -> Adapter -> custom.metrics API -> independent HPA -> /scale
KEDA: Prometheus -> KEDA scaler/metrics server -> KEDA-managed HPA -> /scale
```

`writer_check()` 只筛选指向当前 Deployment 的 HPA/ScaledObject。
Fixed 要求两者都不存在；HPA 要求一个没有 KEDA owner 的 HPA；KEDA 要求一个
ScaledObject 和一个 owner UID 指向它且 `controller=true` 的 HPA。
KEDA 生成的 HPA 属于同一链路。两个独立 HPA 或多一个手工 HPA 都会失败。

默认 Deployment 不允许 parent ownerReferences，避免 KServe reconciliation 混进来。
managedFields 只是历史，所以函数同时返回 `replicas_field_managers` 和说明。
进入动态模式前必须停用 GitOps/manual replicas 同步，并在锁中确认
`replicas_sync_disabled: true`；之后仍需重复采样确认没有竞争写入。

## 3. `switch_scaler()` 怎样交接

先保存完整 Deployment、HPA 和 ScaledObject，再确认目标上的所有 scaler 都属于 lab。
未知 controller 会阻止交接。删除旧 lab ScaledObject/HPA 后等待旧链消失，才安装新资源。
Fixed 模式使用 `/scale` 设置 replicas；HPA/KEDA 模式只安装 controller，不重新 apply
含 replicas 的完整 Deployment，从而避免配置持续覆盖控制器输出。

切换不是分布式事务。如果新 controller apply 失败，session 会保存失败和 after 状态；
使用 `--restore` 返回 Fixed-2，再做 smoke。不要在控制链未收敛时发正式流量。

## 4. 两条路径为何都用 AverageValue

默认候选指标为 Ready 模型 Pods 的 `vllm:num_requests_waiting` 总和，单位 requests。
Prometheus recording rule 输出一个 workload-total series；HPA 的 Object metric 和
KEDA trigger 都采用 `AverageValue`，目标阈值表示每 replica 可接受的 queue 数。

在没有 tolerance、missing metrics 或 stabilization 干预的简化情况下：

```text
desired replicas = ceil(workload total queue / target queue per replica)
```

独立 HPA 使用 `Object` metric，`describedObject` 是同一个 Deployment，经 Adapter 的
`custom.metrics.k8s.io` 提供；KEDA 使用自己的 `external.metrics.k8s.io`。这样不争用同一个
APIService。两条路径 metric type 不同，但此处 workload-total / replicas 的 denominator
一致，仍需保存 API 数值与 HPA currentMetrics 核对。

这不同于把 workload-total 当作每 Pod metric 再乘一遍 replicas。代码让两条路径使用相同
query/target type/阈值和 scaleDown stabilization；poll/sync、缓存和 missing-data 机制仍
不同，因此结论属于完整控制路径比较。

`threshold: 4` 只是待校准候选。正式运行必须填写 `metric.calibration_evidence`，先以
独立 seed 做 low/steady/backlog 校准，再冻结 query、单位、阈值和 scrape label identity。

## 5. 核心代码精读

### HPA 的 AverageValue 计算如何处理总量与副本数

源码阅读样本为 Kubernetes `v1.34.1 / 93248f9ae092f571eb870b7664c534bfc7d00f03`；这不是实验集群版本声明。
`GetObjectPerPodMetricReplicas()` 对应本周 Object metric + AverageValue 路径：

源码：[pkg/controller/podautoscaler/replica_calculator.go](https://github.com/kubernetes/kubernetes/blob/93248f9ae092f571eb870b7664c534bfc7d00f03/pkg/controller/podautoscaler/replica_calculator.go#L304-L318)，第 304–318 行；以下为原文摘录，仅移除公共缩进。

```go
func (c *ReplicaCalculator) GetObjectPerPodMetricReplicas(statusReplicas int32, targetAverageUsage int64, metricName string, tolerances Tolerances, namespace string, objectRef *autoscaling.CrossVersionObjectReference, metricSelector labels.Selector) (replicaCount int32, usage int64, timestamp time.Time, err error) {
	usage, timestamp, err = c.metricsClient.GetObjectMetric(metricName, namespace, objectRef, metricSelector)
	if err != nil {
		return 0, 0, time.Time{}, fmt.Errorf("unable to get metric %s: %v on %s %s/%s", metricName, objectRef.Kind, namespace, objectRef.Name, err)
	}

	replicaCount = statusReplicas
	usageRatio := float64(usage) / (float64(targetAverageUsage) * float64(replicaCount))
	if !tolerances.isWithin(usageRatio) {
		// update number of replicas if change is large enough
		replicaCount = int32(math.Ceil(float64(usage) / float64(targetAverageUsage)))
	}
	usage = int64(math.Ceil(float64(usage) / float64(statusReplicas)))
	return replicaCount, usage, timestamp, nil
}
```

先从 metric API 获取 workload 总量；读取失败返回 error。`replicaCount` 初始保留
当前 status replicas，用总量除以“每副本 target × 当前副本数”判断偏离程度。
超出 tolerance 后，原始建议值是 `ceil(total / target)`；最后把返回的 usage 换算为
当前副本的平均值供状态使用。两次除法目的不同，不能再把总量乘一次 replicas。

手算当前2副本、queue总量12、target=4：ratio=1.5，原始建议为3；但本周
`maxReplicas=2`，最终不会扩成3，积压可能继续增长。总量从8变成8.2时，是否变化还
受实际 tolerance 影响；不能每个采样点都机械地套 ceil 宣称 controller 失效。

随后要沿 HPA controller 的 normalization 继续检查 min/max、扩缩速率和 stabilization。
计算函数给出建议，不负责创建 Pod；Deployment、调度、GPU 资源、模型加载、Ready 和
首 token 是后续不同阶段，必须由冷启动 timeline 分开。

### KEDA 为什么也要检查生成的 HPA

本仓库 `scaling_resources()` 将 Prometheus trigger 显式设置为 `AverageValue`，并把
`horizontalPodAutoscalerConfig.behavior` 写进 ScaledObject。KEDA 提供 external metric，
生成的 HPA 继续执行副本控制；应检查实际 HPA 的 owner UID 与目标 Deployment，不能
把 KEDA 和它自己的 HPA 当成两个竞争 writer。

**设计取舍与边界。** 本周 min=1，不用 KEDA cooldown 的 scale-to-zero 行为解释
2→1；常规 scale-down 还要看生成 HPA 的 behavior。Metric 缺失是获取错误，不是
测得0；不能靠 `or vector(0)` 把 outage 变成缩容信号。每增加一个副本增加 G 张 GPU，
`replicas × G` 与单副本固定 TP 必须一起解释容量和成本。

**读后自检。** HPA desired 已增加但 Pod 仍 Pending 时，哪个阶段尚未完成？如果
新指标失效，怎样区分 controller 保留容量和业务真实无负载？

## 6. Missing 不能变成 zero

`metric_value()` 要求 Prometheus success、vector、恰好一条 series、有限非负值和新鲜
timestamp。empty/multiple series、NaN/Inf、过期和未来时间都会报错。真实的 0 才是零负载。

示例 recording rule 先检查原始样本 freshness，再只选择 Ready Pod；只有每个 Ready Pod
都有唯一的有效 queue sample 时才生成总量。缺失不是 `or vector(0)`。
Pod/model/workload 的 scrape relabeling 必须在固定 `metrics_scrape` artifact 中定义，
防止旧 Pod、重复 scrape、其他模型的 series 混入总量。

`metric_sample()` 同时保留原始 Prometheus response 和 adapter 的 custom metric API
响应。采样失败记 `metric_error`；不会向 HPA 或 KEDA写一个人工零值。HPA currentMetrics、
KEDA generated HPA、operator/events 则由原始对象快照检查。

## 7. 负载、冷启动与 drain

`burst` 的中间三分之一请求用 4 倍到达率；`ramp` 逐渐升到 4 倍再降载；`short_burst`
只在很窄窗口加速。这里 burst 长短最终由保存的 trace 和真实冷启动相对判断，不从名字
推定“短于模型加载”。所有模式复用相同 seed、token shape 和 GPU 上限。

`drain` workload 保留 long 请求，但它本身不能保证恰好覆盖 scale-down。必须从实际
termination timestamp 与 SSE `first_content_at/completed_at` 判断 overlap；没有 overlap
就补跑并记录无效采样。客户端取消测试与 Pod termination drain 是不同实验。

`cold_start()` 输入以 Pod UID 关联的 observed → desired → scheduled → model_ready →
route_eligible → first_token。任一步缺失为 incomplete；顺序错误为 nonmonotonic。
它不会把 Ready 直接补成首 token，也不会把 node Pending/model load 归因到 controller。

## 8. GPU-hours 的积分

`sample_loop()` 只计算 target Pods 中已 scheduled、尚未 Succeeded/Failed 的 GPU requests。
terminating 但仍占用 GPU 的 Pod 仍计数；Pending 且没有 node 的 Pod 不计 allocated。
`allocation_cost()` 对时间线做左阶梯积分，并拒绝逆序、缺值或超过 max-gap 的采样空洞。
采样在 warmup 前开始，`measurement-start.json` 则标记正式流量开始，两种窗口可以分别计算。

例如前半小时分配两张卡，后半小时一张卡，allocated=1.5 GPU-hours。
如果两台 GPU 节点全程运行，实际 billed 仍可能是 2 GPU-hours。没有 provider 或 node
lifecycle 证据时 billed 字段为 null；如果提供 billing JSON，其 start/end 必须与实验
积分窗口一致，并带 source。

## 9. 执行和分析

```bash
bash scripts/run_week20_autoscaling.sh --preflight
bash scripts/run_week20_autoscaling.sh --apply --case fixed_1
bash scripts/run_week20_autoscaling.sh --run --case fixed_1 --workload burst --repeat 0
bash scripts/run_week20_autoscaling.sh --apply --case hpa
bash scripts/run_week20_autoscaling.sh --run --case hpa --workload burst --repeat 0
bash scripts/run_week20_autoscaling.sh --apply --case keda
bash scripts/run_week20_autoscaling.sh --run --case keda --workload burst --repeat 0
bash scripts/run_week20_autoscaling.sh --restore
python3.12 -m src.analyze_platform scaling \
  --input results/week20/SESSION/timeline.jsonl \
  --aux results/week20/SESSION/cold-start.jsonl --end ACTUAL_END_EPOCH \
  --max-gap 30 --output results/week20/SESSION/cost.json
```

Prometheus、adapter API、KEDA scaler 和 KEDA metrics server outage 分开配置 apply/restore
artifacts；empty/stale/multiple/malformed 也是独立 case。入口不自动停止共享监控服务。
Scale-to-zero 不在默认矩阵和生成资源中；先具备独立 activation 指标、零 Pod 可达入口和
首请求合同，再设计隔离实验。

Dashboard 的 client panels 可通过本地 exporter 接入正在运行的 session：

```bash
python3.12 -m scripts.serve_platform_metrics --session results/week20/SESSION --port 8099
```

Exporter 只监听 `127.0.0.1`，固定的 Prometheus 需通过已配置的本地/私有抓取路径读取
`/metrics`。offered counter 来自即时写入的 `clients-arrivals.jsonl`，completed/SLO/TTFT
来自完整 client 行；最终半行未写完时等待下一次 scrape，坏的完整行返回 503。
尚无完成样本时不输出假零 TTFT/SLO。对历史 session 启动 exporter 只提供当前快照，
不会自动把历史事件回填到 Prometheus 时间轴。

## 10. 验证与最终结论

[契约测试](../tests/test_platform_contract.py) 覆盖同一 target/denominator、1–2 上限与
KEDA owner；[分析测试](../tests/test_platform_analysis.py) 覆盖非法 metric、真实 zero、
采样空洞、成本拆分和缺失启动阶段。填写 [week20 报告](../reports/week20.md) 后，才根据
实际 SLO、恢复和运维证据选择 HPA 或 KEDA。代码不预设哪条路径胜出。
