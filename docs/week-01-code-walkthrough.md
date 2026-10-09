# Week 1 Code Walkthrough: Prefill、Decode 与 KV Cache

> 本文解释 Week 1 已实现的实验代码。它描述代码会如何运行，不代表 GPU
> benchmark 已经执行或结果已经产生。

## 1. 这套代码要回答什么

第一周刻意不使用 vLLM，也不直接调用 `model.generate()`。代码通过 Hugging
Face 模型手写 batch size 为 1 的 greedy autoregressive loop，把一次生成拆成：

```text
prompt text
  -> tokenize on CPU
  -> copy tensors to GPU
  -> prefill the complete prompt
  -> select output token 1
  -> decode output tokens 2..N
  -> record latency, throughput, memory, and output identity
```

实验只改变 `use_cache=true/false`，用于回答：复用历史 attention Key/Value
与每一步重算完整序列，在不同 prompt/output 长度下有什么性能差异。这里研究的是
单请求内部的 KV Cache，不是跨请求 prefix caching，也不是 vLLM 的 continuous
batching 或生产服务吞吐。

## 2. 从一条命令到全部产物

学习者入口是：

```bash
make run-week01 PYTHON=.venv/bin/python
```

调用链如下：

```text
Makefile
  -> scripts/run_week01.sh
       -> write_week01_status: running
       -> prepare_week01_model
       -> check_env
       -> src.generate                   # 一次 smoke generation
       -> src.benchmark_kv_cache         # 正式 Cache on/off 矩阵
       -> src.analyze_week01             # 从 CSV 生成两张图
       -> write_week01_status: artifacts_ready

学习者填写 reports/week01.md
  -> make verify
       -> 验证完整证据包
       -> write verification receipt
       -> status: completed
```

`artifacts_ready` 和 `completed` 是两个不同状态。程序成功跑完只表示原始数据和图表
已经准备好；只有人工完成报告且 verifier 通过后，实验才算完成。

相关入口：

- [Makefile](../Makefile)
- [scripts/run_week01.sh](../scripts/run_week01.sh)
- [scripts/verify_week01.py](../scripts/verify_week01.py)

## 3. 配置如何定义实验

[configs/week01.yaml](../configs/week01.yaml) 固定了模型、生成输入和实验矩阵：

```yaml
model:
  id: Qwen/Qwen2.5-0.5B-Instruct
  revision: 7ae557604adf67be50417f59c2c2f167def9a775
  dtype: bfloat16
  local_files_only: true

benchmark:
  warmup_runs: 2
  repeats: 5
  prompt_tokens: [32, 256, 1024]
  output_tokens: [32, 128]
  cache_modes: [true, false]
```

模型 revision 是不可变的 40 位 Hugging Face commit，而不是会漂移的 `main`。
正式矩阵共有：

```text
3 prompt lengths x 2 output lengths x 5 repeats x 2 cache modes
= 60 formal cases
```

每个 `(prompt length, output length, cache mode)` 还会预热两次，因此另有
`3 × 2 × 2 × 2 = 24` 次不写入正式结果的 warmup。

## 4. 模型加载

核心实现在 [src/inference.py](../src/inference.py)。`load_model()`：

1. 拒绝没有 CUDA 的环境。
2. 把配置中的 `bfloat16` 或 `float16` 转成 PyTorch dtype。
3. 从已经验证的本地 Hugging Face snapshot 加载 tokenizer 和模型。
4. 用 `device_map={"": "cuda:0"}` 把模型放在第一张 GPU。
5. 调用 `model.eval()` 关闭训练态行为。

实际生成函数还有 `@torch.inference_mode()`，因此不会构建 autograd graph。
`model.eval()` 和 `inference_mode()` 不是一回事：前者改变 dropout 等模块的运行
模式，后者关闭梯度跟踪及相关开销。

## 5. 如何得到精确长度的 prompt

`build_exact_length_input()` 先 tokenize 配置中的固定文本，再重复 token ID 序列，
最后切到恰好 32、256 或 1024 tokens：

```text
fixed text -> tokenizer -> repeat token IDs -> slice -> [1, target_tokens]
```

这样能严格控制输入 shape，但长输入是合成的重复模式，不是自然长文；它适合受控
性能实验，不适合评价生成质量。由于原始序列已经包含 special tokens，重复序列也
可能把 special token 放到合成 prompt 中间，这应记录为实验限制。

## 6. 一次生成的核心流程

`run_greedy_generation()` 是第一周最值得逐行阅读的函数。

### 6.1 Prefill

完整 prompt 一次送入模型：

```python
prefill_output = model(
    input_ids=device_input,
    attention_mask=attention_mask,
    use_cache=use_cache,
)
```

然后读取最后一个 prompt 位置的 logits，并使用 `argmax` 选择第一个输出 token：

```python
next_token = prefill_output.logits[:, -1, :].argmax(
    dim=-1, keepdim=True
)
```

因此，第一个输出 token 由 prefill 产生。后续 decode loop 实际只执行
`output_tokens - 1` 次。

### 6.2 Cache on

Prefill 返回 `past_key_values`。每个 decode step 只输入最新 token，并带上历史
cache：

```python
step_output = model(
    input_ids=next_token,
    attention_mask=step_mask,
    past_key_values=past_key_values,
    use_cache=True,
)
past_key_values = step_output.past_key_values
```

数据流是：

```text
prompt -> token 1 + KV cache
token 1 + cache -> token 2 + updated cache
token 2 + cache -> token 3 + updated cache
...
```

历史 token 的 K/V 投影无需重复计算。但每个新 Query 仍然要关注越来越长的历史
cache，所以 Cache on 并不意味着每一步成本完全恒定。

### 6.3 Cache off

不保存 `past_key_values`。每生成一个 token，都把 prompt 和已生成 token 拼成完整
序列，再从头 forward：

```python
full_sequence = torch.cat([device_input, next_token], dim=1)
step_output = model(
    input_ids=full_sequence,
    attention_mask=torch.ones_like(full_sequence),
    use_cache=False,
)
```

数据流是：

```text
prompt -> token 1
prompt + token 1 -> token 2
prompt + token 1 + token 2 -> token 3
...
```

输出越长，重复计算历史序列的成本越明显。这就是本周实验要直接观察的现象。

## 7. CUDA 为什么需要同步计时

CUDA kernel 默认异步提交。若直接在 Python 中使用 `perf_counter()`，结束时间
可能只表示 kernel 已经提交，而不是执行完毕。`_measure_cuda()` 因此在调用前后都
执行：

```python
torch.cuda.synchronize()
```

当前计时边界如下：

| 指标 | 包含内容 |
|---|---|
| `tokenization_ms` | CPU tokenizer 加上 repeat/slice/reshape |
| `h2d_ms` | input IDs 和 attention mask 从 host 复制到 GPU |
| `prefill_forward_ms` | 完整 prompt 的 model forward |
| `first_token_selection_ms` | 取最后位置 logits、argmax 和 `token.item()` |
| `inference_ttft_ms` | H2D + prefill + first-token selection |
| `end_to_end_ttft_ms` | tokenization + inference TTFT |
| `decode_ms` | 第 2 个至最后一个 token 的 step latency 总和 |
| `total_generation_ms` | inference TTFT + decode |
| `end_to_end_ms` | tokenization + total generation |

公式集中在 [src/latency.py](../src/latency.py)：

```text
output_tokens_per_second
= output_tokens / total_generation_seconds
```

这个吞吐包含 TTFT，不等于 `1000 / mean_tpot_ms`。TPOT 只统计第一个 token 之后
的 decode steps；若只生成一个 token，就没有 TPOT 样本。

模型加载、warmup、CSV 写入和最终 detokenization 不在这些延迟指标内。

## 8. 核心代码精读

### Decode 闭包怎样把一次计算变成下一步状态

前面的 prefill/cache 分支说明了计算内容；再看循环末尾，理解“模型返回 token”与
“循环推进状态”分别发生在哪里。入口是 `run_greedy_generation()` 中的 `decode_step()`。
下面前五行在闭包内，最后三行在外层循环，保留缩进差异以显示状态交接位置。

源码：[src/inference.py](../src/inference.py)，第 177–185 行；以下为原文摘录，仅移除公共缩进。

```python
    token = step_output.logits[:, -1, :].argmax(dim=-1, keepdim=True)
    token_id = int(token.item())
    if not use_cache:
        full_sequence = torch.cat([full_sequence, token], dim=1)
    return token, token_id

(next_token, token_id), elapsed_ms = _measure_cuda(decode_step)
generated.append(token_id)
decode_step_ms.append(elapsed_ms)
```

逐段读：`argmax(..., keepdim=True)` 保持 `[1, 1]`，使返回值能直接成为下一步输入；
`token.item()` 把单个 token ID 取到 CPU，便于保存结果，也会引入 host/device 同步。
Cache off 分支将刚生成的 token 追加到 `full_sequence`，下一步才重新计算这条更长序列。
闭包返回后，外层更新 `next_token`、追加 `generated`，并保存这一步的延迟。

这里的 `nonlocal past_key_values/full_sequence` 很关键：它们跨 decode 调用保存状态；
`next_token` 则由外层循环在每次调用返回后赋新值。若只更新 `generated` 而漏掉
`next_token`，日志长度仍会增加，但模型会不断消费错误的输入。

手算 `P=4, O=3`：prefill 消费 4 个 token，得到第 1 个输出；两次 decode 分别消费
第 1、第 2 个输出，得到第 2、第 3 个输出。Cache on 的最终 KV 长度为 6，最后一个
输出尚未进入模型。Cache off 的 forward 长度依次是 4、5、6。

**设计取舍与边界。** 每步 `.item()` 让这个教学 loop 易于检查，但不能把其 TPOT 当作
优化后 serving engine 的极限。Greedy `argmax` 也不是 temperature/top-p sampling；
改采样策略需要同时修改一致性验证，不能继续要求与旧 greedy 路径逐 token 相同。

**读后自检。** 如果把 cache-off 的 `torch.cat` 移到下一次 forward 之后，会漏掉哪个
token？如果 `O=1`，为什么没有 decode 样本，却仍然有一次模型调用？

## 9. Benchmark 如何组织 60 行结果

[src/benchmark_kv_cache.py](../src/benchmark_kv_cache.py) 的正式循环是：

```python
for prompt_tokens in prompt_lengths:
    for output_tokens in output_lengths:
        for repeat in range(repeats):
            for use_cache in cache_modes:
                run_one_case()
```

每一行 CSV 的唯一 case key 是：

```text
(prompt_tokens, output_tokens, repeat, use_cache)
```

当前配置总是先运行 Cache on，再运行 Cache off，而没有随机化顺序。相邻执行可以
减少长时间漂移，但固定顺序本身也可能产生 order effect，报告中应把它列为限制。

## 10. 如何证明两条路径算的是同一件事

生成使用 greedy `argmax`。每次运行会保存两个短 SHA-256 hash：完整输出对应
`output_token_hash`，前 `min(32, output_tokens)` 个 token 对应
`parity_token_hash`。对于相同的：

```text
(prompt_tokens, output_tokens, repeat)
```

Cache on/off 的 `parity_token_hash` 必须一致，否则 benchmark 立即失败。BF16 下两条
路径使用不同矩阵 shape，舍入误差可能在较长生成的后段改变某次 greedy argmax；
因此完整 `output_token_hash` 仍保留用于审计，但后 32 token 之后的分叉不会把固定
shape 性能实验误判为实现错误。

这体现了性能实验的基本原则：

> 先证明优化前后在明确的 parity window 内一致，再比较速度；窗口外的数值分叉要
> 作为实验限制保留证据。

该 hash 只是实验一致性检查，不是生成文本的安全或密码学证明。

## 11. Spot 中断后为什么能续跑

[src/result_store.py](../src/result_store.py) 不会直接在 CSV 尾部裸写。每完成一个
case，它会：

```text
read existing rows
  -> write old rows + new row to a sibling temporary file
  -> flush and fsync
  -> atomically replace the CSV
  -> fsync the containing directory
```

因此 VM 在写入时被抢占，已有完整 cases 仍能保留，当前 case 最多重跑一次。恢复
时，程序先校验已有 CSV，再跳过已经存在的 case keys。

这里假设只有一个 benchmark writer；代码没有为多个并发进程写同一 CSV 提供锁。

## 12. 为什么记录这么多 identity

[src/common.py](../src/common.py) 和
[src/week01_contract.py](../src/week01_contract.py) 将结果绑定到：

- Git commit 和关键源码文件 hash；
- model ID、固定 revision 和模型文件清单；
- `model/generation/benchmark` 科学配置 fingerprint；
- Python、PyTorch、Transformers、CUDA、driver 和 GPU；
- 完整 dependency freeze；
- GCE instance 与独立 `run_id`。

输出路径不属于科学配置，因此移动结果目录不会改变实验身份。相反，修改 prompt
长度、cache matrix、模型或依赖都会使续跑被拒绝，避免把两套条件的数据混进同一
份 CSV。

上传到没有 `.git` 的 VM 时，[scripts/upload_to_gcp.sh](../scripts/upload_to_gcp.sh)
会生成 `.experiment-source.json`。VM 会重新校验关键文件集合和 SHA-256，而不是
相信一个裸 commit 字符串。

## 13. Smoke、正式实验和分析各自负责什么

### Smoke generation

[src/generate.py](../src/generate.py) 只跑一次短生成，用于证明：

- 模型快照可加载；
- CUDA 路径可执行；
- tokenization、prefill、decode 和结果序列化能走通；
- 生成文本与结构化指标可以保存。

Smoke 成功不代表正式矩阵已经完成。

### Formal benchmark

`benchmark_kv_cache.py` 运行全部 60 个 cases，验证已有结果能否续跑，并把每次
原始测量写入 `results/week01/raw/kv_cache.csv`。

### Analysis

[src/analyze_week01.py](../src/analyze_week01.py) 按
`prompt_tokens/output_tokens/use_cache` 分组，对五次重复取 median，生成：

- `generation-time.png`：输出长度与总生成时间；
- `output-throughput.png`：输出长度与 output tokens/s。

图表是原始 CSV 的派生产物，不能替代 CSV。

## 14. 最终 verifier 检查什么

[scripts/verify_week01.py](../scripts/verify_week01.py) 要求以下证据全部存在且一致：

- 环境检查通过；
- dependency freeze 与 benchmark runtime 相同；
- 模型 snapshot/revision/fingerprint 一致；
- CSV schema 正确且 60 个 cases 完整；
- Smoke、benchmark、环境和当前源码属于同一实验身份；
- Cache on/off 输出 hash 一致；
- 两张图存在；
- `reports/week01.md` 已删除占位符；
- 报告包含 `run_id`、结果表、图片和带单位的数字。

全部通过后，它写出 `verification-receipt.json` 并把状态从
`artifacts_ready` 更新为 `completed`。Receipt 不把会被随后修改的
`run-status.json` 纳入自身 hash，避免循环依赖。

## 15. 应怎样解释最终结果

合理预期是：

- Cache on/off 的 prefill 时间接近，因为二者都处理完整 prompt；
- Cache on 的主要收益出现在 decode；
- 输出越长，Cache off 重算历史序列的累积代价越大；
- Cache on 的 decode step 仍可能随上下文增长而变慢；
- Cache 使用更多显存，是空间换计算；
- 小模型、batch size 1、无服务队列的结果不能直接代表 vLLM 生产吞吐。

不要预先写“快了多少”。倍率必须来自实际 CSV，并同时说明 GPU、模型、shape、
重复数、失败数和测量边界。

## 16. 推荐阅读顺序

1. [configs/week01.yaml](../configs/week01.yaml)：先理解实验变量。
2. [src/inference.py](../src/inference.py)：重点读 prefill 和两个 decode 分支。
3. [src/latency.py](../src/latency.py)：核对指标定义。
4. [src/benchmark_kv_cache.py](../src/benchmark_kv_cache.py)：理解矩阵和正确性检查。
5. [src/result_store.py](../src/result_store.py)：理解断点续跑。
6. [scripts/run_week01.sh](../scripts/run_week01.sh)：理解整个执行顺序。
7. [scripts/verify_week01.py](../scripts/verify_week01.py)：理解完成标准。

读完后，尝试不看代码回答：

1. 为什么 prefill 已经生成了第一个输出 token？
2. 为什么输出 32 tokens 时只有 31 个 TPOT samples？
3. Cache on 每一步为什么仍需要完整长度的 attention mask？
4. `output_tokens_per_second` 为什么不是 `1000 / mean_tpot_ms`？
5. 为什么 Cache on/off 的前 32 token 不一致时不能继续比较性能，而 32 token 后的
   BF16 分叉可以在保留完整 hash 和限制说明后继续？
6. 为什么 Spot resume 需要同时锁定配置、源码、模型、依赖和 GPU identity？
