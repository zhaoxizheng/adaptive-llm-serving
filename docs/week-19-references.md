# Week 19 Reference Reading

第十九周只补最终实验方法：如何冻结 SLO 口径，以及怎样表达重复实验的不确定性。路由算法、cache 和扩缩容不重新列书目。

新增页面于 2026-09-19 通过公开文档只读抓取核对；阅读范围限定为下列章节，不安排通读 SRE 书或统计教材。

## 新增必读

1. [Google SRE Workbook: Implementing SLOs](https://sre.google/workbook/implementing-slos/)
   - 只读 What to Measure: Using SLIs、Moving from SLI Specification to SLI Implementation、Documenting the SLO。
   - 把已有 TTFT/TPOT 变成可审计的 numerator、denominator、测量窗口和异常分类，不重新学习监控基础。
   - 通用 HTTP 示例不能直接替代 streaming 请求的成功/超时定义。

2. [SciPy `scipy.stats.bootstrap`](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html)
   - 阅读 `paired`、`statistic`、`confidence_level`、`rng`、退化样本警告和示例。
   - 以独立 run 或已定义 block 为重采样单位，不把同一 burst 中相关请求当成独立样本；该 API 不自动完成时间序列 block bootstrap。
   - 三个 repeats 只够观察波动，不能靠增加 resamples 数量制造信息或稳定的 P99 区间；样本不足时明确不做强推断。

## 复用：只查本周增量问题

| 已有来源 | 本周只查什么 |
|---|---|
| [Week 18 references](week-18-references.md) #1 | 论文对照/负载条件与自己的假设是否一致 |
| [Week 16 references](week-16-references.md) #4–6 | 现有算法能力、扩展边界与 benchmark 原始结果格式 |
| [Week 2 references](week-02-references.md) #1–3 | Latency/goodput 定义，不再次讲解推理阶段 |
| [Week 3 references](week-03-references.md) #5 | 尾部风险，仅在解释短请求 SLO 时回查 |

Week 17–18 的配置和报告是本周设计输入；没有结果时应补实验，不能以阅读资料替代证据。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 前周报告；复用 Week 18 #1 | 选定可证伪问题 |
| Day 2 | 新增 1 | 冻结 SLO 和失败口径 |
| Day 3 | 新增 2 的抽样假设 | 数据 split 和重复单位 |
| Day 4–5 | 复用 Week 16 #4–6 | Policy contract 与离线回放 |
| Day 6 | 新增 2 的 paired/退化样本部分 | 比较方法、消融与停止规则 |
| Day 7 | 两份 contract 和用例 | Week 20 handoff |

## 阅读后的自测问题

1. 成功请求的 P99 与全体请求 SLO attainment 为什么必须同时看？
2. Goodput 的 arrival window 与含 drain 的 completion duration 能否混用？
3. Bootstrap 的 resamples 增加，为什么不等于有了更多独立实验？
4. 同一 burst 的请求相关性如何影响不确定性估计？
5. 为什么固定旧 trace 的排序回放不能直接预测新策略的未来 queue？
6. 哪些参数必须在 held-out evaluation 前冻结？
