# KV 生命周期 invariant 与证据边界

实现见 [parse_kv_trace.py](../src/parse_kv_trace.py)，字段采样点见
[kv_trace_payload.py](../scripts/kv_trace_payload.py)。所有计数针对非 null block。

| 检查 | 何时验证 | 可以发现的问题 |
|---|---|---|
| allocate: `0 → 1` 且无旧 hash | pool allocation 后 | 占用 block 被重复分配、eviction 遗漏 |
| touch: `n → n+1` 且 hash 仍映射同一物理 block | prefix acquisition 后 | 旧 hash / partial block 伪命中 |
| release: `n → n-1 ≥ 0`，hash 不变 | free 后 | double free、把 release 当作内容清空 |
| `free_count = count(ref_count == 0)` | 完成一次 pool 操作后 | free queue 与引用数量不一致 |
| request tables 的占用次数等于 pool ref_count | manager 操作返回后 | request/block ownership 漏记 |
| 一步内两条写入路径不能写同一 block | allocate slots 后 | 不允许的可写共享 |
| 命中 token 数为 `len(block_ids) × block_size` | computed lookup 后 | 不完整 block 被记成命中 |
| 命中长度小于 prompt 长度 | computed lookup 后 | 忽略最后 token/logits 重算 |
| evict 后旧 mapping 中没有该 block ID | eviction 后 | stale mapping |
| terminal 后 tables 为空、全部引用释放 | 完整 trace 结束 | finish/abort 后泄漏 |

合法的共享来自完整 prefix block，读共享不等于两个请求可以随意写同一物理 block。
Hash→block mapping 可以包含内容相同的多个物理副本；解析器不强加“一个 hash 只能有
一个 block”的假设。日志中的 `hash_tag` 是原 hash 表示的 16 字符摘要，只用于本次 run
关联；`mapped` 和 `old_mapping_present` 则在 hook 中直接检查真实字典。

```text
ref=0, hash=None → allocate → ref=1, hash=None
                     ↓ full block cache registration
                  ref=1, hash=H
                     ↓ release
                  ref=0, hash=H → touch → ref=1, hash=H
                     ↓ evict + allocate
                  ref=1, hash=None
```

事件只能证明观测到的路径。它们不证明 hash 算法无碰撞、tensor 内容正确、所有 attention
backend 正确，也不覆盖 sliding window、hybrid groups、KV transfer 或 speculative tokens。
`--allow-partial` 仅便于诊断中断 trace，会保留 `unfinished`，不能用于完成标准。
