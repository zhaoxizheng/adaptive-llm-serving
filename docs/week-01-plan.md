# Week 1 Plan: 跑通第一个可复现的 GPU 推理实验

> 时间预算：10–12 小时
>
> 本周主线：Mac 负责开发和记录，云端 NVIDIA GPU 负责执行。使用一个小模型，亲手观察 prefill、decode 和 KV Cache 对延迟的影响。

## 本周目标

第一周不学习 AIBrix，不阅读大段 vLLM 源码，也不追求实现完整推理引擎。只完成一个小而闭环的实验：

1. 建好可重复使用的本地开发 + 云端 GPU 工作流。
2. 理解自回归生成、prefill、decode 和 KV Cache。
3. 在云端 NVIDIA GPU 上跑通 Hugging Face 小模型。
4. 分别测量 prefill latency、decode latency、tokens/s 和显存峰值。
5. 对比启用和禁用 KV Cache 的生成性能。
6. 把代码、环境和原始数据保存下来，形成第一份实验报告。

## 本周完成标准

周末时应当能够展示：

- [ ] 一条命令可以在云端完成环境检查
- [ ] 一条命令可以运行文本生成
- [ ] 一条命令可以运行 KV Cache 对照实验
- [ ] 至少一份包含原始数据的 CSV 或 JSON 文件
- [ ] 一张“输出长度 vs 生成耗时或 tokens/s”的图
- [ ] 一页实验结论，包含环境、方法、结果和限制
- [ ] 能不看资料解释 prefill、decode 和 KV Cache
- [ ] GPU 实例在实验结束后已经停止，且结果已经同步回本地

## 本周技术选择

### 模型

默认使用：

```text
Qwen/Qwen2.5-0.5B-Instruct
```

第一周使用 0.5B 模型的原因：

- 下载和加载快，减少云 GPU 等待成本。
- 足以观察自回归生成和 KV Cache 的性能差异。
- 出错时迭代快，不会把时间浪费在显存和模型加载问题上。
- 第一周的重点是实验方法，不是模型能力。

本周不要切换到多个模型。3B/7B 模型留到后续正式 benchmark。

### GPU

第一周固定使用 GCP Compute Engine `g2-standard-4` Spot：

- 1×NVIDIA L4 24 GB
- 4 vCPU、16 GiB 内存
- 默认 zone：`us-central1-a`
- 100 GB `pd-balanced` 启动盘
- Spot 抢占动作：`STOP`
- Ubuntu 24.04 LTS

如果 `us-central1-a` 暂无容量，先换 `us-central1` 内其他支持 G2 的 zone，或等待后重试。第一周不需要 H100，也不需要多卡。

GCP Spot 被抢占后，启动盘仍保留。benchmark 必须在每个 case 完成后立即持久化，并在 VM 重启后自动跳过已完成 case。停止 VM 不再收取计算费用，但启动盘仍会产生存储费用。

### 软件

- Linux
- Python 3.10 或 3.11
- PyTorch CUDA 版本
- Transformers
- Accelerate
- pandas
- matplotlib

本周暂不安装 vLLM。先把推理过程和测量方法理解清楚，第二阶段再系统使用 vLLM。

## 推荐目录

本周开始建立最终项目仓库时，先创建最小结构：

```text
adaptive-llm-serving/
├── README.md
├── requirements.txt
├── configs/
│   └── week01.yaml
├── scripts/
│   ├── check_env.py
│   ├── bootstrap_gcp.sh
│   ├── gcp_vm.sh
│   ├── upload_to_gcp.sh
│   └── sync_results_from_gcp.sh
├── src/
│   ├── generate.py
│   └── benchmark_kv_cache.py
├── results/
│   └── week01/
│       ├── raw/
│       ├── figures/
│       └── environment.json
└── reports/
    └── week01.md
```

不要把模型权重、虚拟环境或大体积 profiler 文件提交到 Git。

## 每日安排

## Day 1：准备仓库和云端方案（本地，约 1–1.5 小时）

### 要做的事

- [ ] 创建 `adaptive-llm-serving` Git 仓库
- [ ] 建立上述最小目录
- [ ] 添加 `.gitignore`
- [x] 选择 GCP Compute Engine Spot 作为主 GPU 平台
- [x] 固定 `g2-standard-4`、100 GB 持久启动盘和 SSH 工作流
- [ ] 确认 GCP Project 已启用 Billing、Compute Engine API 和 L4 Spot quota
- [ ] 在 GCP Billing 中设置预算告警
- [ ] 给第一周设置 GPU 使用上限：建议不超过 4–6 个计费小时
- [ ] 在 README 记录本周目标和执行命令占位符

### `.gitignore` 至少包含

```gitignore
.venv/
__pycache__/
*.pyc
.env
models/
checkpoints/
*.safetensors
*.pt
*.pth
results/**/raw/*.log
```

### 当天产出

- 一个干净的 Git 仓库
- 一个确定的云 GPU 方案：GCP `g2-standard-4` Spot
- 一个明确的本周计费上限

Day 1 不需要启动 GPU。

## Day 2：理解一次自回归生成（本地，约 1.5–2 小时）

### 需要弄懂的过程

```text
文本
  ↓ tokenizer
prompt token IDs
  ↓ prefill：一次处理全部 prompt token
第一个输出 token + KV Cache
  ↓ decode：每一步处理一个新 token，并复用 KV Cache
后续 token
  ↓ tokenizer.decode
文本输出
```

### 需要写进个人笔记的问题

- [ ] 什么是 autoregressive generation？
- [ ] prefill 的输入和输出是什么？
- [ ] decode 每一步为什么通常只输入一个 token？
- [ ] Key 和 Value 为什么能够缓存，而 Query 通常不跨步缓存？
- [ ] 禁用 KV Cache 后，每生成一个 token 为什么需要重新计算完整序列？
- [ ] TTFT 和 TPOT 分别对应用户体验的哪一部分？

### KV Cache 估算公式

单请求、单序列的近似 KV Cache 大小：

```text
KV bytes ≈
2
× num_layers
× num_kv_heads
× head_dim
× sequence_length
× bytes_per_element
```

其中第一个 `2` 表示 Key 和 Value。使用 GQA/MQA 的模型要使用 `num_kv_heads`，不能直接使用 attention heads。

### 当天产出

在 `reports/week01.md` 写一段 300–500 字的说明，用自己的语言解释 prefill、decode 和 KV Cache，并手算一次模型在某个 sequence length 下的 KV Cache 大小。

## Day 3：第一次启动云 GPU（云端，约 1–1.5 个计费小时）

### 启动前

- [ ] 本地代码已经 commit
- [ ] 已准备环境检查脚本
- [ ] 已设置本次 session 的最长时间
- [ ] 已确认停止实例和删除实例的区别

### GCP 创建与上传

在 Mac 上执行：

```bash
cd /path/to/adaptive-llm-serving
export GCP_PROJECT_ID=<your-project-id>
export GCP_ZONE=us-central1-a
scripts/gcp_vm.sh create
scripts/upload_to_gcp.sh
scripts/gcp_vm.sh ssh
```

VM 首次启动后执行：

```bash
cd ~/adaptive-llm-serving
bash scripts/bootstrap_gcp.sh .
```

驱动安装可能要求重启。重新连接后再次执行同一个 bootstrap 命令，再进行环境检查和 smoke test。完整说明见项目中的 `docs/gcp-spot-setup.md`。

### 环境检查

启动实例后，先记录：

```bash
nvidia-smi
python --version
python -c "import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.get_device_name(0)); print(torch.cuda.is_available())"
```

`check_env.py` 应将以下内容写入 `results/week01/environment.json`：

- UTC 时间
- Git commit
- 操作系统
- Python 版本
- GPU 型号和数量
- NVIDIA driver 版本
- CUDA 版本
- PyTorch 版本
- Transformers 版本

### Smoke test

- [ ] 下载 `Qwen/Qwen2.5-0.5B-Instruct`
- [ ] 使用 BF16；硬件不支持时使用 FP16
- [ ] 完成一次 32 token 左右的 greedy generation
- [ ] 打印输入 token 数、输出 token 数、总耗时和显存峰值
- [ ] 将输出和环境信息同步回本地
- [ ] 用 `scripts/sync_results_from_gcp.sh ./gcp-results` 同步结果
- [ ] 用 `scripts/gcp_vm.sh stop` 停止 GPU 实例

### 当天验收

不能只以“生成了文本”为完成标准。还必须能从本地打开：

- `environment.json`
- 一份生成输出
- 一份运行日志

## Day 4：实现可测量的生成循环（本地，约 2 小时）

### 实现内容

不要只调用 `model.generate()` 然后测一个总时间。使用显式循环分开观察两个阶段：

1. Tokenize prompt。
2. 调用一次 model forward 完成 prefill。
3. 从 logits 中 greedy 选择第一个 token。
4. 保存 `past_key_values`。
5. 后续每一步只传入新 token 和 `past_key_values`。
6. 记录每个 decode step 的延迟。
7. 计算平均 TPOT 和 output tokens/s。

### 计时要求

- [ ] 正式计时前至少 warm up 2–3 次
- [ ] GPU 操作计时前后调用 `torch.cuda.synchronize()`，或正确使用 CUDA Event
- [ ] 不把首次模型下载时间算入推理延迟
- [ ] 分开记录 tokenization、prefill 和 decode
- [ ] 保存每次 run 的原始数据，不只保存平均值
- [ ] 设置随机种子，并使用 greedy decoding 减少随机性

### 输出字段

每次运行至少记录：

```text
run_id
git_commit
model
dtype
use_cache
prompt_tokens
output_tokens
tokenization_ms
prefill_ms
first_token_ms
mean_tpot_ms
p50_tpot_ms
p95_tpot_ms
total_generation_ms
output_tokens_per_second
peak_memory_mb
```

### 当天产出

- `src/generate.py`
- `src/benchmark_kv_cache.py` 的基本框架
- 至少一个不依赖 GPU 的参数解析或结果格式测试

Day 4 不启动云 GPU；先在本地完成代码审查和静态检查。

## Day 5：完成 KV Cache 对照实验（云端，约 1.5–2 个计费小时）

### 实验变量

固定：

- 同一台 GPU
- 同一个模型和 revision
- 同一个 dtype
- 同一个 prompt
- greedy decoding
- 相同 warmup 次数和重复次数

只改变：

```text
use_cache = true / false
```

建议第一轮矩阵：

| Prompt tokens | Output tokens | Cache | Repeats |
|---:|---:|---|---:|
| 32 | 32 | on/off | 5 |
| 32 | 128 | on/off | 5 |
| 256 | 32 | on/off | 5 |
| 256 | 128 | on/off | 5 |
| 1024 | 32 | on/off | 5 |
| 1024 | 128 | on/off | 5 |

如果运行过慢，先完成 32 和 256 prompt tokens 的组合，再增加 1024。不要为了填满矩阵而留下 GPU 空转。

### 注意事项

- KV Cache on：prefill 后，每个 decode step 只输入最新 token 和已有 cache。
- KV Cache off：每个 decode step 输入当前完整序列，不传递 cache。
- 两条路径必须生成相同 token，或至少验证前若干 token 一致。
- 如果输出不一致，先解决正确性问题，不继续做性能结论。

### 当天产出

- 原始 CSV/JSON
- 完整 stdout/stderr 日志
- 环境元数据
- 一份成功/失败实验记录
- 所有结果同步回本地后停止实例

## Day 6：分析结果并画图（本地，约 1.5–2 小时）

### 至少制作两张图

1. 输出长度 vs 总生成时间，区分 cache on/off。
2. 输出长度 vs output tokens/s，区分 cache on/off。

可选第三张图：

3. Prompt 长度 vs prefill latency。

### 分析时回答

- [ ] cache on/off 对 prefill latency 是否应该有巨大差异？为什么？
- [ ] output length 增大时，cache off 的耗时如何增长？
- [ ] cache on 后，decode step latency 是否完全不变？为什么不会？
- [ ] prompt 更长时，prefill latency 和显存如何变化？
- [ ] 测量中有哪些噪声来源？
- [ ] 当前结果能否代表 vLLM 性能？为什么不能直接等同？

### 报告结构

在 `reports/week01.md` 中使用：

```markdown
# Week 1: Prefill, Decode, and KV Cache

## Question
## Environment
## Method
## Results
## Observations
## Limitations
## What I Learned
## Next Week
```

结果部分保留原始数字，结论中避免只写“更快”或“更省显存”，要明确在哪个 workload 下改变了多少。

## Day 7：复盘和验收（本地，约 1 小时）

### 口头复盘

尝试不用资料，在 5 分钟内回答：

1. 一次请求从 prompt 到第一个 token 发生了什么？
2. prefill 和 decode 的计算形态有什么不同？
3. KV Cache 保存了什么？
4. KV Cache 用空间换取了什么？
5. 为什么输出越长，禁用 KV Cache 的代价越明显？
6. 为什么只报告平均 latency 不够？
7. 为什么这次实验还不能代表生产 serving？

回答不清楚的问题就是第二周的复习入口。

### 仓库验收

- [ ] README 中有运行方式
- [ ] 环境和依赖可以重建
- [ ] 原始数据没有被图表替代
- [ ] 图表可以从原始数据重新生成
- [ ] 报告明确记录实验限制
- [ ] Git 状态干净
- [ ] 云端 GPU 已停止
- [ ] 确认没有遗留计费资源

## 本周建议时间分配

| 内容 | 时间 | 运行地点 |
|---|---:|---|
| 概念学习与笔记 | 2 小时 | Mac |
| 仓库和实验设计 | 1.5 小时 | Mac |
| 代码实现 | 2 小时 | Mac |
| 云端环境与 smoke test | 1–1.5 小时 | 云 GPU |
| 正式对照实验 | 1.5–2 小时 | 云 GPU |
| 数据分析和报告 | 2 小时 | Mac |
| 复盘 | 1 小时 | Mac |
| **总计** | **约 11–12 小时** | **云 GPU 约 3–4 小时** |

## 本周不要做什么

- 不阅读完整 vLLM 源码。
- 不安装或部署 AIBrix。
- 不搭 Kubernetes。
- 不写 CUDA/Triton kernel。
- 不比较多个模型。
- 不追求聊天 UI。
- 不把时间花在模型回答质量上。
- 不为了“充分利用 GPU”而临时增加没有实验问题的测试。

## 遇到问题时的处理顺序

### CUDA 不可用

1. 检查 `nvidia-smi`。
2. 检查 PyTorch 是否为 CUDA build。
3. 检查 driver 与 CUDA/PyTorch 兼容性。
4. 优先换用平台提供的成熟 PyTorch 镜像，不在计费实例上长时间修环境。

### 模型加载失败或 OOM

1. 确认没有其他进程占用 GPU。
2. 使用 BF16 或 FP16。
3. 降低 prompt/output length。
4. 保持 0.5B 模型，不要在第一周扩大模型。

### 测量结果波动大

1. 增加 warmup。
2. 每组至少重复 5 次。
3. 使用 CUDA synchronize 或 CUDA Event。
4. 确认 GPU 上没有并发任务。
5. 报告 median/P50 和 P95，不只报告平均值。

### 云端时间快超预算

1. 当前 benchmark 已按 case 增量保存；先中断进程。
2. 用 GCP 同步脚本把结果下载到 Mac。
3. 用 `scripts/gcp_vm.sh stop` 停止实例。
4. 在本地修复代码后再启动新 session。

### Spot VM 被抢占

1. 用 `scripts/gcp_vm.sh status` 确认 VM 状态。
2. 用 `scripts/gcp_vm.sh start` 恢复 VM，并重新 SSH。
3. 再次运行 `make benchmark PYTHON=.venv/bin/python`。
4. 程序会读取相同 CSV 并跳过已经完成的 case；最多重跑中断时正在执行的一个 case。

## 第一周结束后的自然衔接

第二周可以在这套实验框架上增加：

- prompt length 和 output length 的系统化 sweep
- batch size 实验
- 显存组成分析
- TTFT、TPOT、throughput 的更严格定义
- 一个最简 dynamic batching 原型

本周的脚本、结果格式和环境记录会继续复用，不应在第二周推倒重来。
