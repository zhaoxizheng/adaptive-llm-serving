# Week 9 中文代码导读：KV Blocks、Prefix Reuse 与 Eviction

本周代码回答“这个请求持有哪些 KV blocks，它们何时被共享、释放或覆盖”。阅读顺序是
配置 → vLLM hook → parser → 实验结果。配套资料：[学习计划](week-09-plan.md)、
[参考资料](week-09-references.md)、[共同运行约定](week-09-12-runbook.md)、
[报告模板](../reports/week09.md)。

## 1. 代码地图

| 文件 / 函数 | 本周职责 |
|---|---|
| [week09.yaml](../configs/week09.yaml) | cold、exact、partial、pressure 与额外 abort 场景 |
| [deep_study_contract.py](../src/deep_study_contract.py) `make_jobs` | 构造精确长度、精确分叉位置的 token 输入 |
| [build_deep_trace_patches.py](../scripts/build_deep_trace_patches.py) `build` | 在固定源码边界插入 hook，生成 patch |
| [week09-kv-trace.patch](../patches/week09-kv-trace.patch) | 实际应用到 vLLM 的变更 |
| [kv_trace_payload.py](../scripts/kv_trace_payload.py) | manager request context、pool 快照与事件写入 |
| [run_deep_study.py](../scripts/run_deep_study.py) `run_one` / `wait_kv` | 启动独立 engine、顺序发送、等待释放证据 |
| [parse_kv_trace.py](../src/parse_kv_trace.py) `parse_events` | block 状态、request table、引用和 stale mapping 检查 |

完整源码路径见 [KV map](vllm-kv-cache-map.md)，逐项约束见
[KV invariants](vllm-kv-invariants.md)。

## 2. 先手算一个 block table

固定 `block_size=16`。544-token prompt 需要 34 个逻辑 blocks；decode 继续追加后可能
需要更多 blocks。这里的“逻辑第 3 个 block”不是“物理 ID=3”，实际物理 ID 由 pool
分配，request table 保存两者的映射。

Exact 场景的两个请求共享 512 tokens，后面各有不同的 32-token suffix：

```text
first:  [shared 512 tokens][suffix A 32 tokens]
second: [shared 512 tokens][suffix B 32 tokens]
potential reusable blocks = 512 / 16 = 32
```

Partial 场景在第 519 token 后分叉，`floor(519/16)=32`，剩余 7 个相同 token 不构成
完整 block，不能多算一次命中。`make_jobs()` 用同一个 prefix family 生成共同前缀，
再强制两个 suffix 的首 token 不同，避免“预期分叉但实际 token 偶然相同”。

如果把整个 prompt 完全重放，当前版本仍要为最后 logits 重算：最大缓存命中长度是
`prompt_length-1`，再向 block 边界取整。这与“两个 prompt 共享完整长前缀、suffix
不同”的 exact 场景要区分。

## 3. 核心代码精读

### Token 预算最终怎样变成 KV block 分配

沿 vLLM `v0.10.2 / 01efc7ef781391e744ed08c3292817a773d654e6` 的 `KVCacheManager.allocate_slots()` 阅读：

源码：[vllm/v1/core/kv_cache_manager.py](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/v1/core/kv_cache_manager.py#L255-L276)，第 255–276 行；以下为原文摘录，仅移除公共缩进。

```python
# The number of computed tokens is the number of computed tokens plus
# the new prefix caching hits
num_computed_tokens = (request.num_computed_tokens +
                       num_new_computed_tokens)
num_tokens_need_slot = min(
    num_computed_tokens + num_new_tokens + num_lookahead_tokens,
    self.max_model_len)

num_blocks_to_allocate = self.coordinator.get_num_blocks_to_allocate(
    request_id=request.request_id,
    num_tokens=num_tokens_need_slot,
    new_computed_blocks=new_computed_block_list,
    num_encoder_tokens=num_encoder_tokens,
)

if num_blocks_to_allocate > self.block_pool.get_num_free_blocks():
    # Cannot allocate new blocks
    return None

# Touch the computed blocks to make sure they won't be evicted.
if self.enable_caching:
    self.block_pool.touch(new_computed_block_list)
```

先合并该请求已计算的 token 与新命中的 prefix token，再加本轮新 token 和 lookahead，
得到需要可写 slot 的总长度。Coordinator 按实际 KV groups/block size、已有 blocks
和命中引用计算额外分配需求。不要自己用 `ceil(new_tokens / block_size)` 替代它：
部分填充的尾 block 可以继续用，命中的零引用 block 也会占用 free queue 中的容量。

例如单组 full attention、block size=16，已有 16 个已计算 token 的 block，本轮再算
1 个，就必须能提供第 2 个 block；已有 17 个已计算 token 的两个 blocks，再算 1 个
通常仍使用原尾 block。无 lookahead、无新命中时，两者的新 token 数相同，新增 block
需求却不同。

### Free、可复用与 eviction 是三个状态变化

源码：[vllm/v1/core/block_pool.py](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/v1/core/block_pool.py#L260-L267)，第 260–267 行；以下为原文摘录，仅移除公共缩进。

```python
# Materialize the iterable to allow multiple passes.
blocks_list = list(ordered_blocks)
for block in blocks_list:
    block.ref_cnt -= 1
self.free_block_queue.append_n([
    block for block in blocks_list
    if block.ref_cnt == 0 and not block.is_null
])
```

先减少每个 block 的引用数，只把降到 0 且不是 null block 的项放回 free queue。
这里没有删除 hash mapping，也没有立即清零 GPU 内存。另一个请求命中时，`touch()`
会从 free queue 移除零引用 block 并增加引用；若先被 `get_new_blocks()` 取去重用，
`_maybe_evict_cached_block()` 才移除旧 hash mapping。

**设计取舍与边界。** 保留零引用缓存让后来的相同 prefix 仍能命中，又允许 allocator
在需要时回收空间。A/B 共享 block 的引用数从 2 降到 1 时还不能重用；降到 0 才是
eviction candidate。这个逻辑针对已固定的实现，null block 和不同 KV group 要单独看。

**读后自检。** 请求结束后 cache hit 为什么可能仍然存在？画出同一 block 的
`ref_count: 0 → 1 → 2 → 1 → 0`，标出哪些时刻在 free queue 中、何时旧 hash 才失效。

## 4. 为什么不用现成 KV event 原样导出

固定版本的 `BlockStored` event 可以包含 token IDs。本周要求 trace 不记录输入内容，
因此 patch 在原有操作完成后采集 block ID、ref_count、短 hash 摘要与 mapping 状态，
不直接序列化原始 KV event。

`snapshot()` 的核心是检查真实字典，而非只看 block 对象上有没有 hash：

```python
mapped = key is not None and (
    pool.cached_block_hash_to_block.get(key, {}).get(block.block_id) is block
)
```

如果 block 挂着 H，但 hash map 已不指向这个物理对象，`mapped=False` 会使 parser
拒绝这条“缓存仍有效”的解释。`hash_tag` 仅保留摘要，完整 hash 输入不会写入日志。

## 5. Request context 怎样穿过多层调用

`manager_call` 装饰 `get_computed_blocks`、`allocate_slots`、`free`、`cache_blocks`。
启用 trace 后，它把当前 request ID 放进 `ContextVar`，调用原函数，在返回后保存
request block table；`finally` 恢复上层 context。

这样 `manager → coordinator → pool` 的嵌套事件都有同一个 request ID。每个 hook
保留原函数参数、返回值和异常，不替代 allocation 决策。关闭 trace 时直接调用原函数。

每个 scheduler step 开始更新 KV 的 step 编号。它用于阅读该步内的 allocation/free
顺序；request terminal free 也可能出现在该步的 output handling 期间。

## 6. Parser 重建的是状态，不是模拟 allocator

Parser 从 `pool_init` 得到容量和保留的 null block，普通 block 初始都是 `ref=0`。
之后按真实事件更新状态：

```python
expected_ref = previous_ref + {
    "allocate": 1,
    "touch": 1,
    "release": -1,
}.get(event, 0)
```

它不会自行选择“下一次应该分配哪个 block”。每个 manager 返回边界，再把所有
request tables 中的 ID 出现次数加总，与 pool 的 ref_count 对比。这里分两层检查是
必要的：pool 已完成分配但 manager 尚未更新 table 的中间状态，不能误判成泄漏。

完整结束时，所有已出现的 request 都需要 terminal free，所有普通 block 引用都要
归零。`FINISHED_ABORTED` 是合法终止；preemption 的释放不是最终完成，后续仍需恢复
或终止。`--allow-partial` 只放宽结尾完整性，不放宽负引用、stale mapping 等错误。

## 7. 五个场景各验证什么

| 场景 | 控制条件 | 要读的事件 |
|---|---|---|
| cold | 新 engine，544 input / 8 output | miss → allocate → cache → release |
| exact | 同一 engine 顺序发送，共享 512 tokens | 第二个 request 的 computed / touch |
| partial | 共享 519 tokens，block size 仍为 16 | 只复用完整 32 blocks |
| pressure | pool 限为 96 blocks，依次放入 1024-token 不同前缀，再重放近期/早期前缀 | evict、旧 mapping 移除、重新计算 |
| abort | 首个内容 chunk 后关闭长请求，再发新请求 | terminal reason=FINISHED_ABORTED，引用归零，后续成功 |

每个场景独立重启 engine，场景内部按配置顺序发请求。Pressure 的 96 是总 block 数，
还需减去 null block；它只是机制压力配置，不是 Week 6 推荐运行点的新容量结论。
若 abort 处理前请求已正常结束，`observation.json` 会显示未观察到目标 abort，不能
只依据客户端关闭 socket 就宣称已经验证回收。

## 8. 运行和离线解析

先完成共同运行约定中的 Week 9 patch 和源码环境准备：

```bash
make plan-week09 PYTHON=python3.12
PYTHON=.venv-vllm/bin/python bash scripts/run_week09_kv_scenarios.sh --run --scenario partial
```

每次保存在 `results/week09/traces/partial/SESSION_ID/`，其中 `capture/` 包含
`kv-PID.jsonl`、`client.json`、`analysis.json`、`request-block-timeline.csv`、server
metadata 和日志；session 根目录有 `run.json`、`observation.json`。

同步到 Mac 后重新分析：

```bash
python3.12 -m src.parse_kv_trace \
  --trace-dir results/week09/traces/partial/SESSION_ID/capture \
  --output results/week09/analysis/partial.json
```

`prefix_lookups` 给出真实 cached tokens，`request_timeline` 给出每次 allocation 的
requested/computed/cached tokens。用它们对照 client TTFT；trace 的文件写入本身会
增加开销，因此单次 TTFT 差异不能作为生产性能收益。

## 9. 自测与交接

[测试](../tests/test_deep_study.py) 覆盖释放后保留缓存、partial block、旧 mapping、负
引用、free queue 错位、request ownership 和同一步可写冲突。这是 CPU contract 测试，
不代替真实 block 内容验证。

读完应能解释：为什么 ref_count=0 仍能 prefix hit；为什么 cache registration 不等于
GPU 写完；为什么 scheduler preemption 和物理 block eviction 是两件事。Week 10 接手
的输入是 `SchedulerOutput` 和 request block tables，继续追踪到 slot mapping 与 GPU。
