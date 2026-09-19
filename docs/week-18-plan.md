# Week 18 Plan: Cache-aware Routing 与 KV Event 一致性

> 时间预算：约 11 小时
>
> 本周主线：恢复固定双副本，把 Week 13 的实例内 APC 推进到跨副本的 cache locality 路由，并验证缓存信息不新鲜时的正确性与性能边界。
>
> 前置：[Week 16 plan](week-16-plan.md) 的路由基线、[Week 17 plan](week-17-plan.md) 的控制链检查；阅读：[Week 18 references](week-18-references.md)。下列文件是待完成产出。

## 本周目标

1. 区分 KV tensor、block hash、gateway cache index 和路由估计。
2. 在相同副本、硬件、APC 与 gate/blending 设置下比较 load-aware 和 prefix-aware 路由。
3. 核对 tokenization、model identity、hash serialization 与 cache isolation。
4. 观察 block removal、Pod replacement、事件延迟和 index 重建。
5. 找出“缓存更热但排队更长”的反例，为最终策略选定窄问题。

## 本周边界

- 两个独立、同型号 L4 GPU slots，每个 vLLM replica 独占一张；关闭 workload 扩缩容。
- 全部路由组保持实例内 APC 开启，先比较路由，不把 APC 开关变化混入收益。
- 不部署 distributed KV tensor transfer、PD disaggregation 或新存储层。
- 只选一个已支持的 prefix-aware 策略；Preble 用来理解 locality/load tradeoff，不要求复刻论文。
- KV sync 如与固定版本不兼容，保留普通 prefix-routing 实验，并把 event 部分标记 blocked，不能用估计命中冒充实际 KV 命中。

## 本周最终产出

- `configs/week18-cache-routing.yaml`：策略、cache warmup、prefix family、load skew 与 event 设置。
- `docs/cache-routing-contract.md`：identity、hash/event 生命周期、fallback 与隔离边界。
- `scripts/run_week18_cache_routing.sh`：双副本对照、事件 smoke 和 index 验证。
- `results/week18/`：request-to-Pod、预测/实际命中、event lag、queue 和逐请求结果。
- `reports/week18.md`：locality 收益、热点反例、兼容性和最终项目候选问题。

## Cache Identity 与 Evidence Contract

| 对象 | 核对内容 |
|---|---|
| 请求输入 | model/revision、tokenizer/chat template、token IDs、adapter 与可信 cache namespace |
| Block key | parent hash、完整 block tokens、额外 identity/salt、hash 算法及序列化 |
| Residency | Pod UID/进程代次、block stored/removed、实际保留状态 |
| Gateway index | 来源是请求历史估计还是 engine events，更新时间和重建方式 |
| 命中证据 | gateway predicted prefix tokens 与 engine 实际 reused tokens 分开 |
| 性能结果 | cache hit 单位、TTFT/TPOT、queue、goodput、预热成本和路由 CPU 开销 |

不要自行用 prompt 字符串 hash 代替引擎 block key，也不要假设不同进程/版本的 hash 都可直接比较。跨租户只允许显式授权的共享前缀；租户身份由可信鉴权层提供，不能用用户任意填写的 header 或 salt 作为隔离保证。实验只用合成内容，cache hashes/index 也不公开真实请求数据。

## 最小路由矩阵

| 配置 | 副本与 APC | 本周目的 |
|---|---|---|
| Week 16 load-aware baseline | 固定 2，APC on | 同一 gateway 的对照 |
| Prefix-aware，既有 index 模式 | 固定 2，APC on | locality 与负载权衡 |
| Prefix-aware，KV event index | 固定 2，APC on | 在兼容且可证明 source 切换时比较状态精度 |

所有组固定 gateway 的附加 gate/blending。若 index 模式切换还改变 tokenizer、算法或模型参数，先统一这些配置并重跑 baseline；无法隔离时命名为组合变化，不把全部收益归因 event sync。

主矩阵只做前两组；第三组在兼容性 smoke 通过后，用一个共享前缀场景补做，不扩张全因子组合。每个正式 cell 至少三个独立重复，保存样本量。

| Workload | 控制变量 | 要验证的问题 |
|---|---|---|
| Shared-prefix，均匀分布 | 固定 token 长度和共享比例 | 路由是否减少重复 prefill |
| Hot-prefix，偏斜分布 | 少数热门 family，其他输入不变 | 热缓存 Pod 是否因 queue 变差 |
| Low-sharing | 输入/输出长度尽量匹配 | locality 收益消失后的额外开销 |

每组分别记录未预热到稳态的过程；warm-cache 测量统一预热次数、分布和成本。确认负载生成器与 gateway 未饱和，按 short/long 和 prefix family 分组看 TTFT，不能只看总命中率。

## Event 正确性验证

1. 创建一个已知合成 prefix，关联 engine block event、gateway index 和实际复用。
2. 通过受控 cache churn 触发移除，验证 index 不无限保留过期 residency；不能用 refcount 归零直接代表已驱逐。
3. 在实验范围替换一个 Pod，核对 UID/进程代次、旧 entry 清理与新 Pod 的冷缓存。
4. 短暂暂停实验 subscriber，观察 event lag、丢失检测和恢复；PUB/SUB 不能默认具有持久重放保证。
5. 若版本没有可靠 catch-up/rebuild，记录限制并禁用该模式或使用已验证的无 cache-affinity baseline；不补写未经验证的“自动恢复”。
6. 对过期或未知 cache 状态，允许 engine 重新计算而非错误复用；路由偏差只影响性能的前提也须由 identity 与输出检查支持。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 固定双副本与路由附加配置，核对 cache identity 和 event 兼容性 |
| Day 2 | 1.5 h | 合成 prefix 的 store/hit/remove smoke，建立预测与实际命中对照 |
| Day 3 | 2 h | Shared-prefix 主矩阵，记录冷到暖和稳态结果 |
| Day 4 | 2 h | Hot-prefix 与 low-sharing 对照，定位负收益边界 |
| Day 5 | 1.5 h | Pod replacement、事件暂停及恢复，记录 stale index 行为 |
| Day 6 | 1.5 h | 必要重复和分组分析，确定 Week 19 候选问题 |
| Day 7 | 1 h | 完成 contract/报告，同步原始结果并停止计费资源 |

## 报告必须回答的问题

1. Index 表示真实 residency 还是请求历史估计，如何证明？
2. Prefix-aware 路由在什么共享度与负载下有效，什么情况下不如 baseline？
3. Predicted hits 与 engine reused tokens 的差距来自哪里？
4. Cache churn、Pod 重启和 subscriber 中断后，旧状态如何失效？
5. 减少 prefill 的收益是否被 queue、tokenization 或 gateway 开销抵消？
6. 下一个最小改动应解决哪个反例，而不是重复现有 AIBrix 能力？

## 完成标准

- [ ] 主矩阵固定硬件、副本、APC、workload 和路由附加机制。
- [ ] Token/hash identity 与授权共享边界明确，未用字符串相似度冒充命中。
- [ ] 预测命中、实际复用、queue 和客户端性能可关联。
- [ ] 三类 workload 包含负收益检查、独立重复和预热成本。
- [ ] Event 功能的 store/remove/restart/recovery 已验证，阻塞项单列。
- [ ] 输出正确性和 streaming 行为无回归，未知项未写成保证。
- [ ] Week 19 的候选问题、baseline 和证据已保存，GPU 成本收尾完成。
