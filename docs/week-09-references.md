# Week 9 Reference Reading

第九周只围绕 KV block 生命周期阅读：scheduler 如何查询、分配和释放 blocks，以及 prefix cache 如何通过 block hash 复用已经计算的 KV。

源码链接使用 `main` 作为入口发现；执行计划时必须替换为 Week 7 固定 commit 的 permalink。Automatic Prefix Caching 与 PagedAttention 的概念资料分别复用 Week 4 Reference #10 和 #11，不重复列出 URL。

## 必读：KV Cache Manager 源码

1. [vLLM V1 KV Cache Manager Source](https://github.com/vllm-project/vllm/blob/main/vllm/v1/core/kv_cache_manager.py)
   - 追踪 computed block lookup、slot allocation、request free 和 cache event。
   - 记录 scheduler 调用它时传入和返回的真实数据结构。

2. [vLLM V1 Block Pool Source](https://github.com/vllm-project/vllm/blob/main/vllm/v1/core/block_pool.py)
   - 关注 physical block、reference count、free queue、touch 与 eviction。
   - 从代码确认当前淘汰顺序，不从命名猜测实现。

3. [vLLM V1 KV Cache Utilities Source](https://github.com/vllm-project/vllm/blob/main/vllm/v1/core/kv_cache_utils.py)
   - 理解 cache config、KV cache groups 和 block allocation 的辅助逻辑。
   - 只深入当前单模型、单 GPU 场景实际经过的分支。

## 需要复用的前置资料

- Prefix cache 用户语义：复用 Week 4 Reference #10。
- PagedAttention 与内存分页设计：复用 Week 4 Reference #11。
- Scheduler 与 request state：复用 Week 8 Reference #1–2。
- KV cache 大小估算：复用 Week 2 Reference #5。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 1、3，复用 Week 8 #1–2 | manager boundary 与 config |
| Day 2 | 1–2 | lookup、allocate 与 reference |
| Day 3 | 2，复用 Week 4 #10–11 | free、reuse 与 eviction |
| Day 4–6 | 反复对照 1–3 | trace 与四个场景 |
| Day 7 | 固定 commit permalinks | block map 与报告 |

## 阅读后的自测问题

1. Scheduler 在什么时机查询已计算 blocks，又在什么时机申请新 slots？
2. Logical block、physical block 和 request block table 分别是什么？
3. 为什么共享前缀的最后一个 partial block 通常不能像完整 block 一样复用？
4. Reference count 归零、进入 free queue 与 cache mapping 失效是否是同一件事？
5. Block 被重新分配时，旧 hash 如何避免产生 stale hit？
6. Finish、abort 和 preemption 分别经过哪些释放路径？
7. 哪些 trace 证据足以证明 prefix cache 实际减少了 prefill 计算？
