# Week 18 Reference Reading

第十八周新增分布式 prompt scheduling 的论文视角和 block key/隔离设计；APC 入门、KV block 生命周期、AIBrix routing 与 KV events 不重复列书目。

新增页面于 2026-09-19 通过公开文档只读抓取核对。论文机制与所选 AIBrix release 的实现分别验证，论文数字不是本实验收益。

## 新增必读

1. [Preble: Efficient Distributed Prompt Scheduling for LLM Serving](https://arxiv.org/abs/2407.00023)
   - 先读问题定义、locality/load-balancing 策略和评估条件，重点找“仅追求 cache hit 为何不够”。
   - 阅读时固定论文版本；不要求实现论文系统，也不重做前面的 batching 基础。

2. [vLLM Prefix Caching Design](https://docs.vllm.ai/en/latest/design/prefix_caching/)
   - 只读 block hash components、序列化一致性和 Cache Isolation for Security。
   - 与 Week 4 APC 用户语义不同，本周新增的是跨进程 key 一致性和授权共享边界；跳过 Week 9 已掌握的 allocate/free 讲解。
   - Hash/salt 不是身份认证，固定版本是否支持相应隔离需实际核对。

## 复用：只查本周增量问题

| 已有来源 | 本周只查什么 |
|---|---|
| [Week 16 references](week-16-references.md) #5 | Prefix 路由如何与 load gate/blending 交互 |
| [Week 16 references](week-16-references.md) #8 | Event schema、tokenizer 前提、store/remove 和 subscriber 恢复限制 |
| [Week 9 references](week-09-references.md) #1–3 | 实际 block 驱逐时机，区分 free 与 residency 消失 |
| [Week 4 references](week-04-references.md) #7/#10 | Engine cache 指标单位与实例内 APC 边界 |

Cold/warm 流程直接复用 [Week 13 plan](week-13-plan.md)；本周只新增副本归属、热点负载和 event staleness，不再分配一次 APC 基础阅读。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 新增 2；复用 Week 16 #8 | Identity、兼容性与 event contract |
| Day 2 | 复用 Week 9 #1–3、Week 4 #7 | Store/hit/remove 证据 |
| Day 3–4 | 新增 1；复用 Week 16 #5 | Locality/load tradeoff 与反例 |
| Day 5 | 复用 Week 16 #8 | Pod replacement、stale index 和恢复 |
| Day 6–7 | 论文评估条件与本地数据 | 适用边界和项目问题 |

## 阅读后的自测问题

1. Shared prefix 在文本相同的情况下，为什么未必拥有可比较的 block hash？
2. Gateway 预测命中与实际 reused tokens 为什么不同？
3. 为什么 cache hit 更高可能同时导致 P99 TTFT 更差？
4. Free、eviction 和 BlockRemoved 分别意味着什么？
5. Subscriber 重连是否能恢复所有遗漏事件，需要什么证据？
6. KV event sync 与 KV tensor transfer 各自传递什么？
7. Cache salt 能否替代可信租户身份和授权策略？
