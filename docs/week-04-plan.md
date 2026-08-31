# Week 4 Plan: 把 vLLM 当作服务使用

> 时间预算：10–12 小时
>
> 本周主线：在 GCP L4 Spot VM 上安装并启动 vLLM，完成 offline inference、OpenAI-compatible serving 和官方 benchmark。用 Week 3 的 workload 思维测量 continuous batching，但本周不深入 scheduler 源码。

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
- 只改变少数关键参数：`max-model-len`、`gpu-memory-utilization`、`max-num-seqs` 和 `max-num-batched-tokens`。
- prefix caching 只做一个隔离的小实验，不混入主基线。
- 不读完整 scheduler、KV cache manager 或 CUDA kernel；源码阅读从 Week 7 开始。
- 不引入 Kubernetes、AIBrix、多 GPU 或多 replica。

## 本周最终产出

- `requirements-vllm.txt` 或 lock artifact：记录实际安装的 vLLM 版本
- `scripts/start_vllm.sh`：固定 server 参数并打印版本
- `scripts/benchmark_vllm.sh`：保存官方 benchmark 命令与原始 JSON
- `src/openai_smoke.py`：streaming / non-streaming 请求 smoke test
- `configs/week04.yaml`：模型、server 参数和 workload matrix
- `results/week04/raw/`：benchmark JSON、Prometheus snapshot 和环境元数据
- `results/week04/figures/`：吞吐、TTFT、TPOT 和 queue/KV 图
- `reports/week04.md`：vLLM baseline 及与 Week 3 的受控对比

## 环境与版本策略

继续使用 GCP `g2-standard-4` Spot（1×NVIDIA L4 24 GB）。模型从 `Qwen/Qwen2.5-0.5B-Instruct` 开始跑通，再根据显存与时间预算选择 1.5B 或 3B 做正式实验。不要在同一张主图中混合不同模型。

安装前先记录：

```bash
nvidia-smi
python --version
python -m pip --version
```

安装后保存实际解析版本，而不是在报告中只写 `latest`：

```bash
python -m pip install vllm
python -c 'import vllm; print(vllm.__version__)'
python -m pip freeze > results/week04/raw/pip-freeze.txt
vllm --help > results/week04/raw/vllm-help.txt
vllm bench serve --help > results/week04/raw/bench-serve-help.txt
```

vLLM CLI 会演进。仓库脚本必须绑定本周实际验证过的版本；若当前官方文档与已安装版本的 `--help` 不一致，以本机 `--help` 和保存的版本为准。

## 第一个 Server Baseline

以如下命令为起点，并在脚本中显式保存全部参数：

```bash
vllm serve Qwen/Qwen2.5-0.5B-Instruct \
  --host 0.0.0.0 \
  --port 8000 \
  --dtype auto \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.85 \
  --max-num-seqs 16
```

先以 VM 内部 localhost 请求为基线，不把公网网络延迟混入结果。若从 Mac 调试，优先使用 SSH port forwarding，不暴露无鉴权公网 endpoint。

## Workload 设计

主实验固定模型和 server 参数，分别运行：

| Workload | Prompt | Output | 目的 |
|---|---:|---:|---|
| short chat | 128 | 32 | 观察调度和固定开销 |
| balanced | 256 | 64 | 与 Week 3 主实验对齐 |
| long context | 2048 | 32 | 观察 prefill / TTFT |
| generation | 128 | 256 | 观察 decode / TPOT |
| mixed | 分布 | 分布 | 观察 continuous batching |

每种固定 shape workload 都做两类实验：

1. **Closed-loop concurrency sweep**：`[1, 2, 4, 8, 16]`，用于观察同时在途请求增加时的 capacity。
2. **Open-loop request-rate sweep**：以低于、接近和高于稳定 capacity 的速率发送，用于发现排队拐点。

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
- prefix cache hit（只在 prefix 实验中）

不要把 client TTFT 与 server queue time 直接相等。client TTFT 还包含 HTTP、serialization、tokenization 和首个 streamed chunk 的传输开销。

## 每日安排

### Day 1：文档地图与环境冻结（约 1.5 小时，本地）

- [ ] 阅读 Quickstart、Online Serving 和 CLI help
- [ ] 确认 L4、driver、Python 与 vLLM 版本兼容路径
- [ ] 新增 vLLM 专用依赖记录，不破坏 Week 1–3 环境
- [ ] 定义 server readiness、shutdown 和日志保存方式
- [ ] 写出 Week 4 experiment contract

### Day 2：Offline inference 与 API smoke（约 1.5–2 小时，GPU）

- [ ] 用 Python `LLM` API 完成一个 offline generation
- [ ] 启动 `vllm serve` 并等待 health/readiness
- [ ] 发送 non-streaming chat/completions 请求
- [ ] 发送 streaming 请求并记录 first chunk time
- [ ] 检查模型名、token 数和 finish reason
- [ ] 保存 server log 和环境版本后停止 VM

验收：同一模型可以通过 offline API 和 HTTP API 返回有效结果。

### Day 3：官方 benchmark 与可复现脚本（约 2 小时，本地 + GPU）

- [ ] 阅读 `vllm bench serve --help` 并锁定实际参数
- [ ] 为 fixed-shape synthetic workload 编写命令
- [ ] 输出 machine-readable result，不从终端文本手抄数字
- [ ] 将 server config、benchmark config 和 Git commit 一起保存
- [ ] 做 20-request smoke，再扩大 request count

一条示意命令如下；执行时以已安装版本的 `--help` 调整：

```bash
vllm bench serve \
  --backend vllm \
  --base-url http://127.0.0.1:8000 \
  --model Qwen/Qwen2.5-0.5B-Instruct \
  --dataset-name random \
  --random-input-len 256 \
  --random-output-len 64 \
  --num-prompts 200 \
  --request-rate 4 \
  --save-result
```

### Day 4：Concurrency sweep（约 1.5–2 个计费小时）

- [ ] 对四个 fixed-shape workload 跑 concurrency sweep
- [ ] 每个 case warmup 后重复至少 3 次
- [ ] 同时抓取 `/metrics` 和 `nvidia-smi dmon`（辅助证据）
- [ ] 标记 throughput 开始趋平和 P99 开始陡升的位置
- [ ] 每完成一组就同步原始 JSON

### Day 5：Open-loop 与 Week 3 A/B（约 2 个计费小时）

- [ ] 为 balanced workload 找到稳定 request rate
- [ ] 在 capacity 的约 50%、75%、90%、105% 运行 open-loop sweep
- [ ] 复用 Week 3 arrival trace 做最小公平 A/B
- [ ] 加入 mixed-length workload 观察 slot reuse
- [ ] 保存所有失败、timeout 和 rejected 请求
- [ ] 同步结果并停止 VM

### Day 6：参数敏感性与 prefix caching（约 1–1.5 个计费小时）

只做小矩阵，每次改变一个参数：

- [ ] `max-num-seqs`: 4、8、16
- [ ] `gpu-memory-utilization`: 0.75、0.85、0.90
- [ ] `max-num-batched-tokens`: 选择 2–3 个与 workload 匹配的值
- [ ] 共享前缀 workload 下，单独比较 prefix caching off/on

若时间不足，优先完成 `max-num-seqs`，其余移动到 Week 5；不要为了填满矩阵牺牲重复次数和证据质量。

### Day 7：报告与 operating point（约 1.5–2 小时，本地）

至少生成：

1. `concurrency-vs-throughput.png`
2. `concurrency-vs-p99-ttft.png`
3. `request-rate-vs-queue-time.png`
4. `week03-vs-vllm-balanced.png`

选择一个 operating point，例如：

> 在 balanced workload 下，选择满足 P99 TTFT < X ms、P99 TPOT < Y ms 且 error rate = 0 的最高稳定 request rate；对应 server 参数为 Z。

这个 operating point 将作为 Week 5 深入参数实验的基线。

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

## 完成标准

- [ ] offline、non-streaming 和 streaming 三条路径均通过
- [ ] vLLM 和 benchmark 的实际版本、help 与参数已归档
- [ ] 至少完成一个 concurrency sweep 和一个 request-rate sweep
- [ ] 客户端原始结果与 server metrics 可按 run ID 对齐
- [ ] 找到 queue 和 P99 开始明显恶化的负载边界
- [ ] 完成 Week 3 与 vLLM 的 balanced workload 对比
- [ ] 选出满足明确 SLO 的单实例 operating point
- [ ] GCP VM 已停止，结果已同步并绑定 Git commit
