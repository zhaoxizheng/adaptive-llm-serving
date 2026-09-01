# Week 9 Plan: KV Cache Manager、Block 生命周期与 Prefix Cache

> 时间预算：10–12 小时
>
> 本周主线：沿着 Week 8 scheduler 对 KV capacity 的调用继续向下，追踪 logical token 如何映射到 physical KV blocks，解释 block 的分配、引用、释放、缓存和淘汰，并用受控实验验证 prefix cache 命中与内存压力下的真实行为。

## 本周目标

完成本周后，应当能够：

1. 解释 request token、block size、block table 和 physical block 的关系。
2. 追踪 scheduler 如何查询已计算 blocks、分配新 slots 并在请求结束时释放 blocks。
3. 解释 block hash、reference count、free queue 与 prefix cache lookup 的职责。
4. 区分“request 已释放 block”与“cached block 内容立即失效”。
5. 构造 cold、exact-prefix、partial-prefix 和 eviction-pressure 场景。
6. 用 block event trace 检查 double free、引用计数和复用等 invariant。

## 本周边界

- 继续使用 Week 7 固定的 vLLM revision、单 GPU 和同一模型。
- 只研究当前版本启用的 V1 KV cache manager；不同时比较不同 major implementation。
- Prefix cache 的性能收益只做小型机制验证，完整 TTFT/吞吐实验留到 Week 13。
- 不实现新的 eviction policy，不修改 block hash 语义。
- 不从 metrics 名称推断内部行为；block 生命周期以源码和 trace 为准。
- 多模态、encoder cache、KV transfer 和 distributed cache 只记录入口，不进入主线。

## 本周最终产出

- `docs/vllm-kv-cache-map.md`：manager、block pool、hash 与 scheduler 交互图
- `docs/vllm-kv-invariants.md`：block 生命周期和可检查 invariant
- `patches/week09-kv-trace.patch`：默认关闭的低开销 block event trace
- `scripts/run_week09_kv_scenarios.sh`：四个确定性 KV 场景
- `src/parse_kv_trace.py`：重建 request-to-block timeline 并检查 invariant
- `results/week09/traces/`：cold、exact、partial、pressure 原始 trace
- `reports/week09.md`：用源码和实验回答 cache reuse 与 eviction 问题

## KV Cache 生命周期模型

先从固定 revision 的源码填充下图中的真实类与方法：

```text
request tokens
    ↓ group by block_size
logical block hashes ── lookup ──> cached physical blocks
    ↓ cache miss                    ↓ cache hit / touch
allocate new slots <──────────── block pool / free queue
    ↓
request block table
    ↓ model execution
finish / abort / preempt
    ↓
release references ──> reusable or evictable physical blocks
```

不要提前假定“free queue 中的 block 没有缓存价值”或“request finish 会清空内容”。把 cache mapping、引用计数和实际可分配状态分别记录。

## Trace Contract

每个事件至少包含：

```json
{
  "step": 12,
  "event": "allocate_slots",
  "request_id": "r2",
  "requested_tokens": 64,
  "cached_tokens": 32,
  "block_ids": [7, 8],
  "free_blocks_before": 18,
  "free_blocks_after": 16
}
```

只记录 block ID、数量、hash 的短前缀和状态；不记录原始 token、prompt 或完整 hash 输入。

Parser 至少检查：

- 同一时刻物理 block 不被两个不允许共享的写入路径占用。
- reference count 不为负，free block 的占用状态与当前实现一致。
- request finish/abort 后，其引用最终全部释放。
- cache hit 只覆盖完整、可复用的 block，不把未满 block 误算为命中。
- block 被重新分配并覆盖时，旧 hash mapping 不再造成伪命中。

## 四个确定性场景

### Scenario A：Cold baseline

- 清空 engine 状态后发送一个固定 prompt。
- 记录 lookup miss、block allocation、decode append 和 finish。
- 目标：建立最小 request-to-block lifecycle。

### Scenario B：Exact shared prefix

- 顺序发送两个具有完全相同长前缀、不同短后缀的请求。
- 保持模型、sampling 和 block 边界不变。
- 目标：确认第二个请求实际复用了多少完整 blocks，以及跳过多少 prefill tokens。

### Scenario C：Partial block boundary

- 构造只共享一部分前缀、且分叉点落在 block 中间的请求。
- 目标：验证只有哪些 block 可复用，避免把 token-level 相同误认为任意粒度命中。

### Scenario D：Eviction pressure

- 先用多个前缀填充 cache，再制造足够 allocation pressure。
- 随后重放早期与近期前缀。
- 目标：观察 block 何时真正被复用或淘汰，并区分 request release、free-list reuse 与 hash eviction。

## 每日安排

### Day 1：Source map 与数据结构（约 1.5–2 小时）

- [ ] 从 Week 8 scheduler 的 KV 调用点进入 manager
- [ ] 标记 manager、block pool、block table 和 hash helper
- [ ] 记录 block size、num blocks 与 cache config 的来源
- [ ] 为关键类和方法建立固定 commit permalink
- [ ] 写出尚未验证的生命周期假设

### Day 2：Lookup 与 allocation（约 1.5 小时）

- [ ] 追踪 computed block lookup
- [ ] 追踪 cache miss 后需要分配的 slots
- [ ] 解释 full block 与 partial block 的不同处理
- [ ] 找到 block touch、reference increment 和 free-queue update
- [ ] 手算 Scenario A/B 的预期 block 数

### Day 3：Free、reuse 与 eviction（约 1.5–2 小时）

- [ ] 追踪 finish、abort 和 preemption 的释放路径
- [ ] 区分逻辑释放、引用归零和物理内容被覆盖
- [ ] 找到 cache mapping 被加入与移除的位置
- [ ] 确认当前实现的 eviction 顺序，而不是只写“LRU”
- [ ] 整理可由 trace 检查的 invariant

### Day 4：实现最小 KV trace（约 2 小时）

- [ ] 优先复用现有 KV event 或 debug hook
- [ ] 只在 lookup、allocate、touch、free、evict 边界记录摘要
- [ ] 用环境变量控制 trace，默认关闭
- [ ] 实现 parser 和 block ownership timeline
- [ ] 用 Scenario A 校正事件顺序

### Day 5：Prefix reuse（约 1.5–2 个计费小时）

- [ ] 运行 cold、exact-prefix 与 partial-prefix
- [ ] 对比 cached tokens、executed prefill tokens 和 TTFT
- [ ] 检查 block 边界是否符合源码规则
- [ ] 重启 engine 后复测，确认 cache 生命周期范围
- [ ] 不将单次延迟差异扩展为性能结论

### Day 6：Pressure、abort 与回收（约 1.5–2 个计费小时）

- [ ] 运行 eviction-pressure 场景
- [ ] 在请求运行中主动 abort 一次
- [ ] 验证所有引用最终释放且后续请求可继续运行
- [ ] 检查旧 prefix 重放是 hit 还是 recompute
- [ ] 同步 trace 后停止 VM

### Day 7：报告与阶段交接（约 1.5 小时）

- [ ] 完成 request-to-block 和 block-state 两张 timeline
- [ ] 每个结论同时引用 source permalink 与 trace event
- [ ] 解释 scheduler preemption 与 block lifecycle 的连接点
- [ ] 将 worker execution 的输入结构交给 Week 10
- [ ] 记录未覆盖的 distributed KV 分支

## 本周不要做什么

- 不把“KV cache 使用率下降”等同于所有 block 内容立即清空。
- 不用 prompt 文本或完整 token 序列作为 trace 字段。
- 不同时改变 block size、model length、memory utilization 和 workload。
- 不从一次 cache hit 推导生产 workload 的收益。
- 不把 scheduler 的 preemption reason 与 block pool 的 eviction event 混为一谈。
- 不维护长期 fork；instrumentation 保持为可重复应用的小 patch。

## 完成标准

- [ ] KV manager、block pool、hash 和 scheduler 边界绑定固定 revision
- [ ] 能从 request tokens 手算完整 block 数和可能的 prefix hit
- [ ] 四个场景均有可解析 trace
- [ ] Parser 能检查引用、释放与 stale mapping invariant
- [ ] Finish 与 abort 路径均证明 block 引用最终释放
- [ ] Prefix hit 与 executed prefill tokens 建立证据链
- [ ] Request release、cached content 与 eviction 三者已明确区分
- [ ] Week 10 所需的 scheduler output / block table 输入已记录
- [ ] GCP VM 已停止，结果已同步并绑定 Git commit
