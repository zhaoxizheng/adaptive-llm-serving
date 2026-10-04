# Week 2 Code Walkthrough: Static Batch、长度、吞吐与显存

> 本文解释 Week 2 代码和证据契约。它不表示 GPU 实验已经运行，也不包含预期值冒充的结果。

## 1. 实验边界

Week 2 继续使用 Hugging Face eager inference，只测 cache-on greedy decoding。正式矩阵只
改变 prompt length、output length 或 static batch size 中的一个变量。`configs/week02.yaml`
展开为 12 个 workload point，每点 5 个 repeat，共 60 个具名 case。成功、OOM 和普通
error 都保留为 terminal row，便于审计和恢复；但普通 error 永远不能通过正式验收。

## 2. CPU 输入层

`src/batch_inference.py` 不 import PyTorch。`build_static_batch` 接受 token ID lists，
返回等宽的 `input_ids`、`attention_mask`、原长度和 padding 元数据。mask 中真实 token
为 1、padding 为 0；left/right padding 都有测试。

正式 workload 使用 `build_exact_length_batch`。row 0 保留原始 prompt，从而 batch=1 与
Week 1 单请求 loop 输入完全一致；只有 row 1 之后添加 request index。全部序列再重复/截断
到同一个受控长度，因此 batch shape 一致，也不会把完全重复的 prompt 冒充真实 batch。

正式 60-case matrix 前必须写出 `results/week02/smoke.json`。它覆盖配置中的 batch
`[1, 2, 4]`，记录 input/mask/output shape、完整生成 token ID、aggregate/per-request SHA-256，
并要求 batch=1 token ID 与 `src.inference.run_greedy_generation` 精确相等。smoke 或 parity
失败会在正式 CSV 写入前中止。

## 3. GPU backend 为什么单独放置

`src/hf_batch_backend.py` 只在调用 GPU 函数时 lazy import Torch 和 Transformers。这样
配置、公式、CSV contract、分析和测试在没有 CUDA 的 Mac 上仍可 import 和执行。

一次测量的路径是：

```text
CPU tokenize/pad/mask/tensor
  -> synchronized H2D
  -> one batched prefill forward
  -> vectorized argmax for every batch row
  -> N-1 cache-on batched decode forwards
  -> copy generated token matrix to CPU and hash it
```

代码没有使用 `.item()` 读取一个 batch 的单个 token。prefill 选 token 时，先由 mask 找到
每一行最后一个非 padding 位置，再对每一行独立 argmax；decode 则直接使用
`logits[:, -1, :]`。每一步把新的 mask column 追加为 1，并复用 `past_key_values`。

## 4. 计时契约

所有 CUDA 区间在开始和结束都执行 synchronize，避免只测到异步 kernel launch：

- `preprocessing_ms`：tokenize、定长构造、padding、mask 和 CPU tensor；
- `h2d_ms`：两个输入 tensor 的复制；
- `gpu_ttft_ms`：第一次 model forward；
- `mean_tpot_ms` / `p95_itl_ms`：第一个 token 之后的 N-1 次 decode interval；
- `generation_ms`：第一次 forward 加 N-1 次 decode；
- `e2e_latency_ms`：preprocessing + H2D + generation。

`summarize_batch_latency` 是独立 CPU 函数，验证 interval 数量并计算 aggregate output
tokens/s 和 requests/s。这里的 latency 是整批完成时间，不是 online request P95。

## 5. 显存口径

模型加载完成并清理 allocator cache 后，记录 allocated/reserved baseline。每 case reset
peak stats，结束时同时读取 peak allocated/reserved，并保存绝对 bytes、MiB 和相对 baseline
delta。理论 KV Cache 使用：

```text
2 × num_hidden_layers × num_key_value_heads × head_dim
  × (prompt_tokens + output_tokens - 1) × batch_size × bytes_per_element
```

`src/kv_cache_estimator.py` 优先读取显式 `head_dim`，否则验证
`hidden_size % num_attention_heads == 0` 后推导。理论值只覆盖 K/V tensors，不应该和
PyTorch allocator delta 完全相等。

## 6. 原子写盘与恢复

`src/week02_contract.py` 固定 45 列 raw schema、配置 fingerprint、runtime/source identity、
60-case key set 和跨字段方程。`src/result_store.py` 每追加一行都会重写 sibling temporary
file、fsync、atomic replace，再 fsync directory。恢复时先验证已有 CSV 的全部行，然后按
`(sweep, batch, prompt, output, repeat)` 跳过 terminal case。

OOM/error row 保留 phase、exception type/message 和当时能读取到的 memory peak；所有 latency
和 throughput field 必须为空。普通 error 保留证据但无法通过 official completion。OOM 只有
在一个 workload 的 5 次正式尝试全部 OOM，并且是 sweep 中至少一个成功点之后的单调 suffix
时才是有效 capacity boundary。混合 success/OOM 不聚合，也不能验收。warmup error 不再复制
为 5 个未执行的 repeat；warmup OOM 后仍逐个执行正式尝试。Spot preemption 或 Ctrl-C 不会
被转写成失败结果；重启后继续缺失 case。

## 7. 分析与报告

`src/analyze_week02.py` 先要求完整 60-row matrix，再按 workload 汇总 completed repeats 的
median 和 nearest-rank P95。capacity-limited point 不生成 latency/throughput 聚合。分析生成：

1. `prompt-length-vs-ttft.png`
2. `output-length-vs-e2e-latency.png`
3. `batch-size-vs-token-throughput.png`
4. `batch-size-vs-latency-memory.png`

每张图标注固定变量、GPU、model、dtype 和 terminal failure count。`reports/week02.md` 初始
只是带 marker 的模板；必须从同步回 Mac 的证据填写。`scripts/verify_week02.py` 会重新验证
raw rows、重算 summary、核对 smoke 与 source/runtime identity，并拒绝任何未删除 marker。
`analysis.json` 绑定 raw、metadata、smoke、summary、每张图的 SHA-256，以及由 summary 与
model/dtype/GPU context 规范化得到的 plot-input SHA-256。报告中的 `WEEK02-EVIDENCE` block
必须绑定同一组证据。PNG byte hash 防止图片被替换，plot-input hash 则绑定可重画输入。

## 8. 执行顺序

在已 commit 的 GCP checkout 中：

```bash
PYTHON=.venv/bin/python CONFIG=configs/week02.yaml bash scripts/run_week02.sh
```

脚本准备 pinned model snapshot、检查 CUDA 环境、运行/恢复 benchmark、生成分析产物，并把
status 写成 `artifacts_ready`。同步 results 并停止 VM 后，在 Mac 填写真实报告，再执行：

```bash
python3 -m scripts.verify_week02 --config configs/week02.yaml
```

只有 verifier 写出 receipt 并把 status 更新为 `completed`，本周证据包才算完成。
