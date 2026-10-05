# 第 17–20 周代码运行手册

四周的实验代码、配置、分析器与中文导读已经提供。真实 GPU、Gateway、EPP、KServe、
HPA/KEDA 实验仍需在独立集群执行；生成代码和 CPU 测试不代表 conformance、性能或成本结论。

| 周 | 入口 | 中文导读 | 主要分析 |
|---|---|---|---|
| 17 | `scripts/run_week17_gaie.sh` | [GAIE / reference EPP](week-17-code-walkthrough.md) | 选择与 dispatch、失败模式 |
| 18 | `scripts/run_week18_llmd_routing.sh` | [llm-d 路由](week-18-code-walkthrough.md) | paired workload、identity、实际 reuse |
| 19 | `scripts/audit_week19_llmisvc.sh` | [KServe 资源审计](week-19-code-walkthrough.md) | UID DAG、composition、删除边界 |
| 20 | `scripts/run_week20_autoscaling.sh` | [HPA/KEDA](week-20-code-walkthrough.md) | 唯一 writer、时间线、allocated/billed |

## 1. 先在本地检查

依赖复用仓库 Python 3.12、PyYAML 和 pytest。不会自动安装依赖或创建云资源。

```bash
make plan-week17 plan-week18 plan-week19 plan-week20 PYTHON=python3.12
python3.12 -m pytest -q tests/test_platform_contract.py tests/test_platform_analysis.py tests/test_platform_runner.py
bash scripts/run_week17_gaie.sh --render --case healthy --output /tmp/week17-template.yaml
bash scripts/run_week20_autoscaling.sh --render --case hpa --output /tmp/week20-hpa.yaml
```

`--plan` 不访问集群，也不加载模型。`--render` 只生成模板；第 19 周必须先提供与所选
release 对齐的声明文件，才能 render。它不会自行猜测 alpha `apiVersion`。

## 2. 版本与资源合同

共用 [serving-platform-baseline.yaml](../configs/serving-platform-baseline.yaml) 是 APC-on 候选，
需要重新完成生成/性能 smoke，再填写 `selection_evidence`、镜像 digest、`near_slo_rps`，
最后设置 `frozen: true`。它不修改第 13–16 周的 baseline。

[cluster-lab.yaml](../configs/cluster-lab.yaml) 填入明确 kube context、独立 namespace、
private listener、GPU 容量与 Gateway/controller/data-plane 固定版本。每个 replica
为 `1 Pod / 1 node / G GPUs / TP=G / PP=1`；两个 replica 可位于不同节点。

第 17–18 周统一使用 `Deployment/vllm` 的两个完整 replica，为第 20 周提供同一个
`/scale` target。第 16 周的 `vllm-a/vllm-b` 迁移到它之前，保存原始证据并显式清理旧实验
Deployment 和有冲突的 route；运行器不会自动删除旧 workload。迁移后重跑 Service
baseline，再引入 EPP。GPU shape、engine/model 与 Gateway 仍须一致。

模型入口为 identity sidecar 的 `8081`；`8000` 暴露 engine health/metrics，供固定版本的
EPP/Prometheus 配置使用。这个 metrics 端口只在独立实验网络内开放。EPP 的 gRPC 端口
是另一个端口，默认候选为 `9002`，必须与真实 release 配置一致。

## 3. 带校验和的 release artifacts

共用锁是 [platform-versions.lock.yaml](../deploy/platform-versions.lock.yaml)；KServe
使用独立的 [versions.lock.yaml](../deploy/kserve/versions.lock.yaml)。初始均 `frozen: false`。

1. 从学习计划固定的 release 保存 CRD、渲染 manifests、chart values 与兼容说明。
2. 填写相应 component 的 `release`、40 位 `source_commit` 与 `images` digest 列表。
3. 为当前周 `required_artifacts` 每项登记本地文件；所有相对路径以仓库根目录为基准。
4. 核对配置和真实安装组合后设置 `frozen: true`。一个文件更新后必须重新绑定和审查。

```bash
python3.12 -m scripts.pin_platform_artifact \
  --key gaie_crds --path deploy/gaie/gaie-v1.0.0-crds.yaml
python3.12 -m scripts.pin_platform_artifact \
  --key reference_epp --path deploy/gaie/reference-epp.rendered.yaml
```

以上路径是需要从固定 release 导出的输入文件；仓库没有伪造官方 CRD、镜像 digest 或
precise-prefix 插件配置。`pin_platform_artifact` 会重新将锁标成 unfrozen。
`compatibility` 可指向审查后的 Markdown，说明固定组合、来源和未支持项。

GAIE 必须是 `v1.0.0`。`reference_epp` 与 `gateway_glue` 分别提供 reference EPP
Deployment/Service/RBAC 和所选 provider 所需的 namespaced glue；GatewayClass、Gateway
及 CRD/controller 安装由前置阶段完成。不要在 artifacts 中重复生成器已有的
Deployment/vllm、InferencePool/vllm、HTTPRoute/inference-platform。

运行器只 apply 明确 namespace 且有 `app.kubernetes.io/part-of: serving-study` 的资源。
CRD、集群 RBAC、APIService、Webhook、Secret 和 Namespace 安装不属于这个 apply 接口。
平台依赖先按固定 release 单独安装并保存证据，然后用 `--preflight` 检查。

## 4. 每个 cell 的执行方式

```bash
bash scripts/run_week18_llmd_routing.sh --preflight
bash scripts/run_week18_llmd_routing.sh --server-dry-run --case load_aware
bash scripts/run_week18_llmd_routing.sh --apply --case load_aware
bash scripts/run_week18_llmd_routing.sh --run --case load_aware \
  --workload shared_prefix --rate low --cache cold --repeat 0
```

`--run` 执行一个 cell；按 `--plan` 输出顺序切换 case，覆盖 repeats 0、1、2。
第 18 周为 4 workloads × 2 rates × 2 cache states × 2 pipelines × 3 repeats。
这避免后台悄悄改变路由配置，也使中断后能精确补跑缺失 cell。
`--output` 对运行操作指定结果根目录；其下总是新建唯一 session，不覆盖已有运行。

冷/热 cell 都显式 restart `Deployment/vllm` 并等待当前 generation Ready。
先对每个 Pod 执行不共享 leading token 的 model warmup；warm cell 再对每个 Pod 预热
每个 prefix family。这里测量的是相同预热条件下的 steady-state；自然形成 locality 的
cold-to-warm 行为看 cold cell 时间序列，不把两种预热合同混成一组。

每次 run 保存 `run.json`、config/baseline/lock、Git/source identity、artifact 副本、
`commands.jsonl`、before/after 对象清单、clients、summary、warmup 与 timeline。
源状态包含 dirty 标记；版本锁和 artifacts 则单独保存内容。成功/失败/中断都更新状态。

只保存合成 family ID、计数、时间和结果；client JSONL 不保存 prompt IDs、生成文本或
完整 cache key。raw Gateway/EPP/engine 日志应先按合同归一化，关闭 body/debug logging。

## 5. 故障与恢复

内建故障包括 EPP 缩到零、pool selector/port 修改、单 Pod membership 与 Pod replacement。
release-specific 故障读取 `fault_<name>_apply` 和 `fault_<name>_restore` 两个带校验和文件，
例如 `fault_adapter_outage_apply`。它们必须是受限 namespaced 资源，不是任意 shell 命令。
恢复 artifact 应为故障前同一 release 的完整期望声明；先在无流量时审查一次 dry-run。

`--fault` 使用 `try/finally` 恢复，并保存故障快照和恢复 smoke。进程被 SIGKILL、Mac
断电或集群失联时，finally 无法保证执行；根据 session 中的原始对象和锁恢复该实验资源。
故障、取消和客户端过载结果不进入正常性能 cell。没有完整 dispatch 日志窗口时，
“未看到后端请求”只能是 unknown，不能证明 FailClose。

## 6. 不由本地测试代替的验收

- CRD admission/defaulting、Gateway/EPP 实际协议、conformance 子集与错误行为。
- 每个 Pod 的真实 GPU UUID/local rank；继续使用 `scripts/verify_replica_shape.py`。
- llm-d 实际 pipeline、load/cache freshness、engine request-level reuse 和输出 identity。
- KServe 字段 composition/provenance、生成对象与删除链；CPU stub 不代表 GPU generation。
- HPA/KEDA live metrics、唯一 writer、长 SSE drain 和账单；未提供 billing 时输出 null。

四周报告初始状态都是 `not_run`。只有上述证据到齐后，才把学习计划的完成项勾选为完成。
