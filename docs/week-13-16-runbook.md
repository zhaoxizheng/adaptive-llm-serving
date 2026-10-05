# 第 13–16 周运行约定

四周代码已经提供；GPU counter、性能、多卡与集群实验仍需在真实环境执行。
仓库中的 `reports/week13.md` 到 `reports/week16.md` 是待填报告，不能当作测量结果。

## 环境与版本

- 本地分析：Python 3.12、现有 PyYAML/pytest。网关测试另使用
  [requirements-gateway.txt](../requirements-gateway.txt) 中的 aiohttp。
- GPU：沿用 [第 9–12 周运行约定](week-09-12-runbook.md) 的 vLLM commit 和完整 patch chain。
  `--run` 验证 editable import、patch 内容；`--plan` 不导入 CUDA/vLLM/transformers。
- 第 13 周保持单卡 L4；第 14 周 TP=1/2 使用同一台双卡机器，配置列出的 device IDs
  决定 `CUDA_VISIBLE_DEVICES`，不与另一种机型的单卡结果计算 speedup。
- 第 15–16 周使用独立 namespace/context。先把
  [serving-baseline.yaml](../configs/serving-baseline.yaml) 的 `frozen`、selection evidence、
  image digest、GPU topology，以及 [cluster-lab.yaml](../configs/cluster-lab.yaml) 的环境锁补齐。
  这些字段默认未填写，代码不会猜测实际 image/controller 版本。

不需要 GPU 的检查：

```bash
make plan-week13 plan-week14 plan-week15 plan-week16 PYTHON=python3.12
python3.12 -m pytest -q tests/test_serving_experiments.py tests/test_rr_gateway.py
```

## 四周入口

| 周 | 配置 | 执行入口 | 中文导读 |
|---|---|---|---|
| 13 | `week13-kernel.yaml` / `week13-prefix.yaml` | `run_week13_ncu.sh` / `run_week13_prefix.sh` | [Kernel 与 APC](week-13-code-walkthrough.md) |
| 14 | `week14-optimization.yaml` / `week14-tp.yaml` | `run_week14_optimization.sh` | [优化与 TP](week-14-code-walkthrough.md) |
| 15 | `week15-multireplica.yaml` | `run_week15_baseline.sh` | [RR 与生命周期](week-15-code-walkthrough.md) |
| 16 | `week16-l7-matrix.yaml` | `run_week16_gateway.sh` | [Gateway API](week-16-code-walkthrough.md) |

每个 shell 入口都要求明确模式，不带 `--run` / `--apply` 不执行硬件工作。
`make run-week13` 跑 prefix 矩阵；ncu 单独运行，避免把 counter 采集混入容量结果。

## 固定与保留的证据

本地服务 runner 每个 cell 启动新进程，模型 warmup 与 prefix priming 分开，保存
`trace.json`、`client.jsonl`、`run.json`、`server.json`、metrics、GPU sample 和 summary。
请求 trace 只包含合成 token IDs，不读取真实对话。返回的合成输出保留 hash 和有限摘要，
用于发现输出异常；token 数通过不等于完成语义质量验收，仍需复用 Week 6 质量检查。

near-SLO RPS 默认 `null`，必须来自已有测量。只试低负载可传 `--rate low`。
TTFT 从预定到达时刻计时，包含 load generator 延迟；TPOT 按首/末内容 chunk 与实际
要求的输出 token 数计算，是客户端估计，不是逐 token GPU 时间。chunk 可能携带多个 token。

测量窗口从首个到达基准到最后一个请求完成，包含 drain 尾部。失败、超时和
`client_overload` 留在分母。不要与采用固定墙钟窗口的其他工具直接比较 goodput。
每个正式配置至少三个重复；P99 成功样本少于 1000 时自动标为探索性结果。

## 集群操作顺序

1. 本地 `--render --output ...` 生成模板；不连接集群。
2. 在独立实验集群安装并固定需要的 GPU plugin / Gateway API controller。
   本仓库不自动安装 controller，也不创建 GPU node pool。
3. 补齐两个 YAML 锁；先核对 namespace、配额、节点可分配 GPU 和入口 private 策略。
4. `--preflight` 保存版本、nodes/quota 与资源快照；人工核对碎片化、其他 Pod 占用和预算。
5. `--server-dry-run --case ...` 检查 API schema/admission，再 `--apply --case ...`。
6. 用显式 context 的 `kubectl port-forward` 接入 ClusterIP；选择 `--cache-state cold/warm`
   执行正式重复。两种模式会在每个 cell 前滚动重启实验 Deployment，并逐 Pod warmup。
7. 保存客户端与后端 identity 日志，另保存固定 controller 的 access logs/capability 证据。
8. 运行离线分析，填报告；同步结果并核对 GPU 节点、磁盘、LB、公网 IP 的残余计费。

`--cache-state mixed` 保留当前进程状态，适合 lifecycle/burst 观察；这类重复不保证独立
cache 初始状态，报告必须披露。HPA 的初始副本数需在每次 burst 前核对，代码不会把
上一轮已经扩到两个副本的状态伪装成 1→2 cold start。

从 Week 15 切换 Week 16 前清理本实验的旧 Deployment/HPA，避免重复占用 GPU；
从 HPA 切回固定副本前先移除本实验 HPA。不要让多个控制器同时写同一个 `/scale`。

## 离线分析

```bash
python3.12 -m src.analyze_serving_study \
  --root results/week14/optimization --output results/week14/tables
python3.12 -m src.analyze_routing \
  --clients results/week15/SESSION/r0-short/client.jsonl \
  --attempts results/week15/SESSION/after/GATEWAY_UID-gateway.jsonl \
  --rr --output results/week15/analysis
```

`--attempts` 一次只使用一个 telemetry 层：RR gateway 或 backend identity 或归一化后的
controller access log。把它们合并会把同一次请求的不同跳误算为 retry。
日志截断、重复/缺失 request ID、没有 route attribution 都是证据缺口，不应写成通过。

## 当前自动化的边界

代码提供可执行采集与验证入口，不自动宣布课程完成：NVTX/section 支持需在实际 ncu
版本确认；graph-only isolation、TP rank mapping、质量和 Pareto 选择需要原始证据复核。
GPU-seconds 的本地边界是 server 生命周期，集群 `gpu_seconds()` 按采样的已调度 Pod
积分；云 VM 闲置、磁盘与 LB 的账单需另外补齐。仅凭 Ready 或 YAML 不能完成性能验收。
