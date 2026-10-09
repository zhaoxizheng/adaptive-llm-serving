# Week 4 Plan: 把 vLLM 当作服务使用

> 时间预算：11 小时
>
> 本周主线：在 GCP L4 Spot VM 上安装并启动 vLLM，完成 offline inference、OpenAI-compatible serving 和官方 benchmark。用 Week 3 的 workload 思维测量 continuous batching，但本周不深入 scheduler 源码。
>
> 代码导读：[Week 4 Code Walkthrough](week-04-code-walkthrough.md)。
>
> 证据状态：本计划描述已实现的 workflow 和待执行实验，不声称 L4 benchmark 已运行，也不包含任何 GPU 结果。

## 本周目标

完成本周后，应当能够：

1. 使用固定版本的 vLLM 启动 offline inference 和 OpenAI-compatible API server。
2. 区分 offline throughput benchmark 与 online serving benchmark。
3. 使用 request rate 和 concurrency 生成 open-loop / closed-loop 压力。
4. 从客户端结果和 `/metrics` 同时解释 TTFT、TPOT、E2E latency、queue time 和 KV cache usage。
5. 在相同 GPU、模型和 workload 下，对比 Week 3 request-level batching 与 vLLM。
6. 找到 vLLM 单实例的可用 operating point，而不是只追求最高 tokens/s。

## 本周边界

- 先把 vLLM 当作黑盒服务正确使用。
- 固定 `max-model-len`、`gpu-memory-utilization`、`max-num-seqs` 和 `max-num-batched-tokens`；本周 executable contract 不做参数敏感性 sweep。
- 本周不运行参数敏感性或 prefix caching 实验；后续归属见“后续交接”。
- 不读完整 scheduler、KV cache manager 或 CUDA kernel；源码阅读从 Week 7 开始。
- 不引入容器编排、集群级网关或路由、多 GPU 或多 replica。

## 本周最终产出

- `requirements-vllm.txt`：固定 `vllm==0.10.2` 和本周分析依赖
- `scripts/start_vllm.py`：校验配置并安全管理 server lifecycle
- `scripts/benchmark_vllm.sh`：保存官方 benchmark 命令与原始 JSON
- `src/openai_smoke.py`：streaming / non-streaming 请求 smoke test
- `configs/week04.yaml`：模型、server 参数和 workload matrix
- `results/week04/raw/`：benchmark JSON、Prometheus snapshot 和环境元数据
- `results/week04/figures/`：吞吐、TTFT、TPOT 和 queue/KV 图
- `reports/week04.md`：vLLM baseline 及与 Week 3 的受控对比

## 环境与版本策略

继续使用 GCP `g2-standard-4` Spot（1×NVIDIA L4 24 GB）。本周固定 `Qwen/Qwen2.5-0.5B-Instruct` 及其 immutable revision，不在正式矩阵中切换模型。

Week 1–3 保留 `.venv`；Week 4 必须使用独立 `.venv-vllm`，避免 vLLM 的 PyTorch/CUDA 依赖改变前几周环境。仓库固定 `vllm==0.10.2`，正式运行前用 bootstrap 创建并归档解析后的环境：

安装前先记录：

```bash
nvidia-smi
python --version
python -m pip --version
```

```bash
bash scripts/bootstrap_vllm_gcp.sh .
.venv-vllm/bin/python -c 'import vllm; print(vllm.__version__)'
```

bootstrap 会保存 dependency freeze 与三个 CLI help surfaces。vLLM CLI 会演进；若当前官方文档与固定 `0.10.2` 的 `--help` 不一致，以归档的本机 `--help` 为准。

## 执行入口

Mac 上可先做不启动 GPU 或 vLLM 的 plan（需要普通 `.venv` 中的 PyYAML）：

```bash
make plan-week04 PYTHON=.venv/bin/python
```

plan mode 只校验 contract 并打印 server、smoke 和 benchmark 计划；它不创建 environment/run ID，不启动 vLLM，也不构成 CUDA compatibility 或性能证据。在 L4 VM 完成 bootstrap 后运行：

```bash
make run-week04
```

精确顺序是：GPU run → 同步证据 → 停止 VM → 离线分析 → 完成报告 → 生成并用真实外部证据填写资源审计 → 离线验证。同步并停止 VM 后，使用普通 `.venv` 生成分析：

```bash
make analyze-week04 PYTHON=.venv/bin/python
```

完成 `reports/week04.md` 后，创建未确认的审计模板：

```bash
make audit-template-week04 PYTHON=.venv/bin/python
```

模板不会自行证明资源已停止。必须用实际命令或 console 证据填写它；随后执行：

```bash
make verify-week04 PYTHON=.venv/bin/python
```

`verify-week04` 使用普通 `.venv` 做离线验证，不要求安装 vLLM 或访问 CUDA。它是 post-run evidence gate，不是 smoke test；缺少完整 matrix、Week 3 trace replay comparison、metrics、图表、数值报告或资源审计时应当失败，未确认的 audit template 也必须被拒绝。

## 第一个 Server Baseline

配置固定绑定 `127.0.0.1:8000`。使用 lifecycle wrapper 预览并启动完整 argv：

```bash
.venv-vllm/bin/python scripts/start_vllm.py --config configs/week04.yaml plan
.venv-vllm/bin/python scripts/start_vllm.py --config configs/week04.yaml start
```

本周默认 contract 只监听 loopback；非 loopback bind 会被拒绝，除非操作者显式确认。正式请求在 VM 内访问 `http://127.0.0.1:8000`，不把公网网络延迟混入结果。若从 Mac 调试，使用 SSH port forwarding，不开放无鉴权公网 endpoint：

```bash
gcloud compute ssh \
  "${GCP_SSH_USER:-llmlearner}@${GCP_VM_NAME:-adaptive-llm-week01}" \
  --project="$GCP_PROJECT_ID" --zone="$GCP_ZONE" \
  -- -N -L 8000:127.0.0.1:8000
```

## Workload 设计

主实验固定模型和 server 参数，分别运行：

| Workload | Prompt | Output | 目的 |
|---|---:|---:|---|
| short chat | 128 | 32 | 观察调度和固定开销 |
| balanced | 256 | 64 | 与 Week 3 主实验对齐 |
| long context | 2048 | 32 | 观察 prefill / TTFT |
| generation | 128 | 256 | 观察 decode / TPOT |
| mixed | 分布 | 分布 | 保留给后续可选实验；不进入当前 primary matrix |

每种固定 shape workload 都做两类实验：

1. **Closed-loop concurrency sweep**：`[1, 2, 4, 8, 16]`，用于观察同时在途请求增加时的 capacity。
2. **Open-loop request-rate sweep**：balanced workload 使用 `[1, 2, 4, 8]` requests/s，用于发现排队拐点。

当前 primary contract 共 72 cases：四种 fixed-shape workload 的 60 个 closed-loop cases，加 balanced workload 的 12 个 open-loop cases；每个点重复 3 次。mixed workload 已定义，但本周 runner 不把它加入 primary evidence。

Week 3 与 Week 4 的正式 A/B 只比较 balanced workload，并固定：

- 同一 GPU 型号和数量
- 同一模型 ID、revision 和 dtype
- 同一 prompt/output token 数
- 同一 request count、warmup 和 arrival trace
- 相同成功条件与 timeout

框架不同导致 tokenizer、sampling 或输出 token 可能不同；正式对比使用固定 token 长度、greedy decoding，并保存实际 input/output token 数。

## 指标与证据

### 客户端指标

- request throughput
- output token throughput
- P50/P95/P99 TTFT
- P50/P95/P99 TPOT
- P50/P95/P99 E2E latency
- success、timeout 和 error count

### Server 指标

- running / waiting request 数
- request queue time
- prompt / generation token throughput
- GPU KV cache usage

不要把 client TTFT 与 server queue time 直接相等。client TTFT 还包含 HTTP、serialization、tokenization 和首个 streamed chunk 的传输开销。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 冻结 vLLM 版本、环境、server lifecycle 与实验 contract |
| Day 2 | 1.5 h | 完成 offline、non-streaming、streaming smoke 与版本证据 |
| Day 3 | 2 h | 固化 benchmark wrapper、machine-readable 输出和 20-request smoke |
| Day 4 | 2 h | 运行四种 fixed-shape closed-loop concurrency sweep |
| Day 5 | 2 h | 运行 balanced open-loop sweep 与 Week 3 trace replay A/B |
| Day 6 | 1 h | 做离线分析、证据完整性检查与 run identity 对账 |
| Day 7 | 1 h | 完成报告、operating point、资源审计与离线验收 |

### Day 1：文档地图与环境冻结（1.5 小时，本地）

- [ ] 阅读 Quickstart、Online Serving 和 CLI help
- [ ] 确认 L4、driver、Python 与 vLLM 版本兼容路径
- [ ] 新增 vLLM 专用依赖记录，不破坏 Week 1–3 环境
- [ ] 定义 server readiness、shutdown 和日志保存方式
- [ ] 写出 Week 4 experiment contract

### Day 2：Offline inference 与 API smoke（1.5 小时，GPU）

- [ ] 用 Python `LLM` API 完成一个 offline generation
- [ ] 启动 `vllm serve` 并等待 health/readiness
- [ ] 发送 non-streaming chat/completions 请求
- [ ] 发送 streaming 请求并记录 first chunk time
- [ ] 检查模型名、token 数和 finish reason
- [ ] 保存 server log 和环境版本后停止 VM

验收：同一模型可以通过 offline API 和 HTTP API 返回有效结果。

### Day 3：官方 benchmark 与可复现脚本（2 小时，本地 + GPU）

- [ ] 阅读 `vllm bench serve --help` 并锁定实际参数
- [ ] 为 fixed-shape synthetic workload 编写命令
- [ ] 输出 machine-readable result，不从终端文本手抄数字
- [ ] 将 server config、benchmark config 和 Git commit 一起保存
- [ ] 做 20-request smoke，再扩大 request count

单 case 先用 wrapper 查看由固定 contract 生成的精确命令；完整矩阵由 `make run-week04` 驱动：

```bash
bash scripts/benchmark_vllm.sh \
  --config configs/week04.yaml \
  --case-id <case-id> \
  --plan
```

### Day 4：Concurrency sweep（2 个计费小时）

- [ ] 对四个 fixed-shape workload 跑 concurrency sweep
- [ ] 每个 case warmup 后重复至少 3 次
- [ ] 为每个 case 保存执行前后的 `/metrics` snapshot，并与 run/case identity 对齐
- [ ] 标记 throughput 开始趋平和 P99 开始陡升的位置
- [ ] 每完成一组就同步原始 JSON

### Day 5：Open-loop 与 Week 3 A/B（2 个计费小时）

- [ ] 为 balanced workload 找到稳定 request rate
- [ ] 按 contract 在 1、2、4、8 requests/s 运行 open-loop sweep
- [ ] 复用 Week 3 arrival trace 做最小公平 A/B
- [ ] 仅在 primary 完整后把 mixed-length workload 作为可选追加实验
- [ ] 保存所有失败、timeout 和 rejected 请求
- [ ] 同步结果并停止 VM

### Day 6：离线分析与证据完整性（1 小时，本地）

- [ ] 在结果同步并停止 VM 后运行 `make analyze-week04`
- [ ] 检查 72 个 primary cases、每点 3 次重复和 terminal status 是否齐全
- [ ] 对齐 raw JSON、normalized records、Prometheus snapshots、server process identity 与 run metadata
- [ ] 检查 Week 3 trace replay comparison 的 source identity、arrival trace 和成功/失败分母
- [ ] 生成四张规定图表，并确认报告中的数值可由保存证据重算
- [ ] 将缺失、重复、跨 run 或未完成的 artifact 标为验证失败，不用补写或手工推断替代

### Day 7：报告与 operating point（1 小时，本地）

至少生成：

1. `concurrency-vs-throughput.png`
2. `concurrency-vs-p99-ttft.png`
3. `request-rate-vs-queue-time.png`
4. `week03-vs-vllm-balanced.png`

选择一个 operating point，例如：

> 在 balanced workload 下，选择满足 P99 TTFT < X ms、P99 TPOT < Y ms 且 error rate = 0 的最高稳定 request rate；对应 server 参数为 Z。

同一 request rate 的三个 repeat 必须完整且全部满足 SLO，才能入选；报告保留各次结果，
并使用选中负载中 TTFT 最差的 repeat 作为具体证据行，不能只展示最好的一次。

这个 operating point 将作为 Week 5 可观测性、SLO 与容量实验的固定起点；Week 5 产出的 baseline 和 load points 再交给 Week 6 参数敏感性实验。

## 后续交接

- [Week 5 plan](week-05-plan.md) 先冻结 metrics、SLO、goodput、prefix cache 背景指标和单实例容量 baseline，不在 Week 4 证据中切换 engine 参数。
- [Week 6 plan](week-06-plan.md) 使用 Week 5 固定的 load points 承接 engine 参数敏感性；prefix caching off/on 也只作为隔离的 follow-up，固定 workload、cache 初始状态和预热成本，不与 Week 4 primary baseline 混合。
- 后续继续复用 [Week 4 references](week-04-references.md) #8–10，不新增或重复外部 reference 条目。

## 对比时必须解释的差异

1. Week 3 一个 request batch 在整个 decode 期间保持固定；vLLM 可以在 iteration 边界重新调度。
2. Week 3 的 padding 和 batch completion 逻辑可能浪费计算；vLLM 使用自己的 scheduler 和 KV cache 管理。
3. 两者的内部 queue time 定义不同，因此优先比较 client-observed 指标和最终 goodput。
4. “vLLM 更快”不是完整结论；必须说明在哪个 workload、负载与 SLO 下改善多少。

## 本周不要做什么

- 不使用未固定版本的 nightly build 做正式基线。
- 不把模型下载和 server cold start 计入 steady-state TTFT。
- 不开放无鉴权公网 vLLM endpoint。
- 不用只跑一次的峰值 throughput 作为结论。
- 不同时调整四个 engine 参数后猜测原因。
- 不在不同模型、GPU 或 token 长度之间做直接框架 A/B。
- 不因 Spot 抢占丢弃失败 case 或拼接半次 run。
- 不把 `--plan`、CLI readiness 或本地测试写成 GPU compatibility 或性能结果。
- 不把当前 runner 未执行的参数敏感性或 prefix caching 写成 Week 4 结果。

## 完成标准

- [ ] offline、non-streaming 和 streaming 三条路径均通过
- [ ] vLLM 和 benchmark 的实际版本、help 与参数已归档
- [ ] 至少完成一个 concurrency sweep 和一个 request-rate sweep
- [ ] 客户端原始结果与 server metrics 可按 run ID 对齐
- [ ] 找到 queue 和 P99 开始明显恶化的负载边界
- [ ] 完成 Week 3 与 vLLM 的 balanced workload 对比
- [ ] 选出满足明确 SLO 的单实例 operating point
- [ ] GCP VM 已停止，结果已同步并绑定 Git commit
