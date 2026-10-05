# Week 17 中文代码导读：GAIE InferencePool v1 与 Reference EPP

本周在相同 Gateway 和双副本模型上建立 EPP 数据路径，用逐请求证据验证选择和失败语义。
对应 [学习计划](week-17-plan.md)、[参考资料](week-17-references.md)、
[运行手册](week-17-20-runbook.md)、[协议合同](inferencepool-epp-contract.md)。

## 1. 从哪里开始读代码

| 文件 / 函数 | 负责什么 |
|---|---|
| [week17-inferencepool.yaml](../configs/week17-inferencepool.yaml) | Service/EPP 对照与故障矩阵 |
| [platform_contract.py](../src/platform_contract.py) `pool_resource` | selector、targetPorts、endpointPickerRef |
| 同文件 `data_plane` | 完整 replica、identity/metrics Service 与 HTTPRoute |
| 同文件 `check_pool_schema` | 检查所选 CRD 的 FailClose 默认值与 enum |
| [run_platform_study.py](../scripts/run_platform_study.py) `schema_preflight` | 固定 CRD 与实际 served schema 对照 |
| 同文件 `injected_fault` | 故障变更及 finally 恢复 |
| [analyze_platform.py](../src/analyze_platform.py) `epp_analysis` | EPP selection 与实际 dispatch 关联 |
| [base.template.yaml](../deploy/gaie/base.template.yaml) | 可审查的基础资源模板 |

建议按配置 → 渲染 → preflight → 一次请求 → 故障 → 分析顺序阅读。Shell 文件只负责定位
仓库根目录和 Python 入口，核心行为都在 Python 中。

## 2. `pool_resource()` 的三个边界

`selector.matchLabels.app=vllm` 选择模型 Pod；`targetPorts.number=8081` 指向 identity
proxy，它把请求送入同 Pod 的 vLLM；`endpointPickerRef.port.number=9002` 指向 EPP gRPC。
后两个端口用途不同。Engine `8000` 仅供实验网内 health/metrics 访问。

```text
Client -> Gateway / HTTPRoute -> ext_proc -> reference EPP
                              <- selected Pod endpoint
       -> selected Pod:8081 identity proxy -> localhost:8000 vLLM -> SSE
```

Pool 里的每个 endpoint 都是完整的 `G`-GPU replica。Router 不选择 rank，也不修改 TP。
两个 Pods 由 `Deployment/vllm replicas=2` 管理；它们可以分别位于两个节点。

`failure_mode=None` 的代码含义是完全省略字段，不是写成 YAML null。
`check_pool_schema()` 要求固定 schema 确认默认 `FailClose`；运行后还要检查 admitted object
和真实故障请求。默认值检查只证明 API 声明，无法单独证明 dataplane 已实现行为。

## 3. 为什么不内置 reference EPP 镜像

GAIE v1.0.0 的 CRD artifact 不包含一个已经验证的 reference EPP 安装组合。
`artifact()` 从锁读取实际文件，验证 SHA-256 后再解析。
`render()` 合并基础对象、`gateway_glue` 与 `reference_epp`，不猜 provider 的 ext_proc 字段。

`scoped_resources()` 检查独立 namespace、lab 标签和重复对象，阻止把 CRD/controller 安装
混入实验 apply。`schema_preflight()` 导出实际 CRD，比较 OpenAPI schema；GatewayClass
Accepted 和 Gateway Accepted/Programmed 都必须属于当前 generation。

模板锁故意未冻结。填入实际版本之前执行 `--run` 会失败，并把原因写到唯一 session 的
`run.json`，不会开始发流量。

## 4. `epp_analysis()` 怎样避免误判

每个 client request ID 关联两种独立证据：EPP decision 和 gateway dispatch。
identity 日志可帮助确认 Pod UID，但不能再作为同一次 gateway attempt 重复计数。

```json
{"request_id":"r1","selected_pod_uid":"pod-a","ready_candidate_uids":["pod-a","pod-b"]}
```

```json
{"request_id":"r1","pod_uid":"pod-a","attempt":1}
```

一个 decision、一个 dispatch，Pod UID 相同且位于 Ready candidates 才为 `matched`。
选到 A、实际送到 B 为 `mismatch`；retry 或重复记录为 `multiple_attempts`；缺日志为
`incomplete`。`matched` 只描述路径一致，client 的 generation 成败仍在 `client_status`。

FailClose 故障下出现成功或 dispatch 是异常；客户端 HTTP error 且确认覆盖完整 dispatch
窗口才标 `closed`。`--complete-dispatch-window` 是对已收集日志范围的明确声明，不应为
了得到通过结果而设置。FailOpen 的 dispatch 只标 `fallback_observed`，不意味着 EPP 正常。

## 5. 故障注入如何恢复

`injected_fault()` 先读取当前对象，只改变一个实验字段，并在 finally 恢复。
EPP unavailable 保存原 replicas、设为 0、等待 Pod 退出后发流量，最后恢复原 replicas。
Membership 给一个 Pod 临时标签，使 selector 只命中它，然后还原 selector 和标签。
wrong_port/no_endpoints 则分别修改目标端口和候选选择条件。

EPP timeout 依赖所选 provider 的 timeout/网络行为，因此从固定的 apply/restore artifacts
接入。Unavailable 不能替代 timeout。故障期间保存对象和客户端结果，恢复后再做一次
generation smoke。Pool/status 收敛和 EPP 重新 Ready 的实际时刻需从时间线核对。

## 6. 按顺序运行

先完成运行手册中的版本与 Gateway 准备，再执行：

```bash
bash scripts/run_week17_gaie.sh --preflight
bash scripts/run_week17_gaie.sh --server-dry-run --case healthy
bash scripts/run_week17_gaie.sh --apply --case healthy
bash scripts/run_week17_gaie.sh --run --case healthy --repeat 0
bash scripts/run_week17_gaie.sh --run --case healthy --workload long --cancel-smoke
bash scripts/run_week17_gaie.sh --apply --case fail_open
bash scripts/run_week17_gaie.sh --fault --case fail_open
python3.12 -m src.analyze_platform epp \
  --input results/week17/SESSION/clients.jsonl \
  --decisions results/week17/SESSION/decisions.jsonl \
  --aux results/week17/SESSION/dispatches.jsonl \
  --output results/week17/SESSION/epp-analysis.json
```

`service_baseline` 与 `healthy` 交错执行三个重复。故障 case 的 apply 安装正常 pool 和所选
failureMode，实际破坏操作只发生在 `--fault`，避免在 preflight 前就失去正常基线。

官方 conformance 使用 `conformance_job` 锁定一个 reviewed Job；必须 `backoffLimit: 0`
并设置不超过 1800 秒的 `activeDeadlineSeconds`。执行 `--conformance` 后保存 Job status
和输出，报告具体 profile/测试范围、pass/fail/skip；代码不会把普通请求 smoke 命名为
conformance，也不会自动创建其 cluster-scoped RBAC。

## 7. 测试与交接

[契约测试](../tests/test_platform_contract.py) 覆盖 failureMode 默认值、端口和完整 replica；
[分析测试](../tests/test_platform_analysis.py) 覆盖不完整日志与实际 dispatch；
[运行器测试](../tests/test_platform_runner.py) 确认流量异常仍恢复 replicas。

填写 [week17 报告](../reports/week17.md)，把 Gateway、Pool、合成 workload、失败语义与
归属 schema 交给 Week 18。Reference EPP 的 conformance/学习定位保持不变。
