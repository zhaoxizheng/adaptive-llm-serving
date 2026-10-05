# llm-d Router / EPP 实验合同

实现入口见 [Week 18 中文导读](week-18-code-walkthrough.md)。llm-d 选择完整 replica
endpoint；vLLM scheduler 负责实例内 token 调度。固定双副本，不启用 autoscaler。

必须冻结 `llmd_load`/`llmd_prefix` rendered resources、chart values、source commit、images，
以及 `load_pipeline`/`prefix_pipeline` 的插件顺序。无法在固定 release 支持 precise path
时，该 cell 保持 blocked，不切换 moving main，也不自造相似算法替代。

| 证据 | 字段/含义 |
|---|---|
| Request | request_id、workload、prefix_family、token counts、arrival/first/completion timestamps |
| Decision | selected_pod_uid、ready_candidate_uids、score/reason、routing_latency_ms |
| Load | metric 名/单位、sample timestamp、load_age_seconds、refresh、missing/stale 处理 |
| Cache prediction | predicted_tokens、index 来源/状态；不能替代 engine 观测 |
| Engine reuse | request_id、reused_tokens、identity；没有观测时为 null |
| Identity | model_revision、tokenizer_revision、template_revision、adapter_id、namespace、pod_uid、process_generation |

Identity 所有字段必须出现，未使用 adapter 用显式 `none` 表达；不能依赖可伪造客户端
header 做租户授权。两个 identity 不一致时禁止归因同一次 cache reuse。

Low-sharing 和 shared-prefix 长度相同；同一 repeat 的 arrival trace 相同。
Hot-prefix 保留 locality 导致排队退化的反例。Cold 与预热后 steady-state 独立呈现；
正式样本按 overall、short/long、family 分组，失败请求保留在分母。

Fault cases 保存 apply/restore 与最终 generation smoke。缺失 load、stale load、cache churn、
Pod replacement、EPP timeout 分开；无法观测实际 eviction 时明确 unknown。
恢复 load-aware 后重新 smoke；不把 fail-open fallback 请求计为正常 pipeline 成功证据。
