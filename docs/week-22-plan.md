# Week 22 Plan: Capstone：Held-out、故障、发布与 Runbook

> 时间预算：约 11 小时
>
> 本周主线：冻结本项目的 capstone stack，在未用于调参的 held-out workloads 上完成最终对照，并用故障注入、受控 rollout/rollback 和值班 runbook 收尾；portable data plane 是 vLLM + Gateway API + GAIE + llm-d，KServe 是可选 alpha 声明式 owner，扩缩容恰好选择 HPA 或 KEDA 一条 writer path（WVA 仅兼容时选做）。
>
> 前置：[Week 18 plan](week-18-plan.md) 的路由策略、[Week 19 plan](week-19-plan.md) 的声明式控制面、[Week 20 plan](week-20-plan.md) 的单 writer 扩缩容、[Week 21 plan](week-21-plan.md) 的 GKE live path；阅读：[Week 22 references](week-22-references.md)。下列文件均为计划产出，不代表 benchmark、故障或发布已经完成。

## 本周目标

1. 在查看结果前冻结版本、SLO、held-out 数据、矩阵、重复数、停止条件与失败规则。
2. 对比固定/自适应路由与固定/HPA/KEDA 组合，只运行能回答主问题的最小矩阵。
3. 验证 Pod、Gateway、EPP/router、metric source 与 rollout 故障时的正确性、可观察性和恢复。
4. 通过零流量预检、shadow/mirror（实现支持时）、小比例 canary、promotion/rollback 完成安全发布演练。
5. 交付可执行 runbook、SLO dashboard/alerts、原始证据和明确限制，不挑选最好的一次 run。

## 本周边界

- Portable data plane 绑定 vLLM、Gateway API、GAIE 与 llm-d Router/EPP 的固定 releases；KServe alpha owner 和 HPA/KEDA/WVA 属于本次实验选择的 control-plane adapters，不把它们写成稳定、必选的 portability API。GKE 仅是执行环境，不将结论外推到未运行云。
- 主张只基于 held-out seeds/prefix families/arrival traces；calibration/development 数据可解释机制，但不进入最终效果数字。
- 不在看到 held-out 结果后改阈值、权重、超时或排除失败；任何修复产生新版本，并按预设规则重跑受影响 baseline/candidate。
- 不引入 PD disaggregation、多节点 KV transfer、新模型、tenant fairness 或 node autoscaling；这些不属于 capstone 收尾。
- 故障注入只在独立实验 namespace/cluster、合成数据、显式 blast radius 和自动恢复上限内执行；不破坏 shared CRD/controller。
- Request mirroring 仅在固定 Gateway implementation 支持且能避免双重副作用/计费时选用；镜像成功不替代 canary 响应验证。

## 本周最终产出

- `docs/experiment-methodology.md`：计划冻结 SLO、cohort、held-out split、矩阵、统计方法与变更规则。
- `configs/week22-capstone.yaml`：计划绑定 releases/images/model、策略、autoscaling、workloads、seeds 和预算。
- `scripts/run_week22_capstone.sh`：计划执行 preflight、baseline/candidate、故障、rollout、rollback 和 cleanup。
- `deploy/rollout/`：计划保存 candidate route/weights、timeouts、可选 mirror、promotion 与 rollback manifests。
- `dashboards/week22-slo.json` 与 `alerts/week22-slo.yaml`：计划关联请求 SLI、route/EPP、replicas、Pod/GPU 和 rollout version。
- `runbooks/adaptive-serving.md`：计划提供触发条件、诊断、缓解、回滚、验证、升级和复盘步骤。
- `results/week22/` 与 `reports/week22.md`：计划保存 raw data、run manifest、失败、成本、统计输出、限制和最终结论。

## Frozen Evaluation Contract

在第一条 held-out 请求前写入只读 run manifest：

| 维度 | 冻结内容 |
|---|---|
| Build | Git commit、KServe/KEDA/GAIE/llm-d/vLLM releases、image digests、CRD checksums |
| Environment | GKE/Kubernetes/GatewayClass、GPU/node、driver、quota、region、网络入口 |
| SLO | TTFT/TPOT 阈值、成功/错误/超时分类、measurement window、drain rule |
| Data | held-out seeds、prefix families、short/long mix、burst schedule、warmup 与 exclusion rules |
| Matrix | baseline/candidate、唯一 replicas writer、路由/扩缩容配置和预设消融 |
| Statistics | independent run unit、重复数、paired ordering、interval method 与 minimum sample caveat |
| Cost | allocated GPU-hours、billed GPU/node-hours、LB/disk 与成功达标 token 分母 |

`SLO attainment = 成功且同时满足该请求适用 TTFT/TPOT 阈值的 offered requests / 全部有效 offered requests`。Latency quantiles 若只对成功请求计算，必须并列 error/timeout；goodput 的 arrival window 与 completion/drain duration 分开。三个重复可显示波动但通常不足以支撑稳定 P99 置信区间，不能靠增加 bootstrap resamples 制造独立信息。

## 最小 Held-out Matrix

所有 cell 使用相同模型、硬件上限、gateway timeout、cache initialization 和 workload manifest；每个正式 cell 至少三个独立、交错顺序的 run。为使 4 小时 GPU 窗口可执行，正式矩阵固定为 8 个 configuration/workload cells、共 24 个 runs：

- Fixed-2 的 Baseline A/Candidate A 分别运行 mixed、shared-prefix Zipf 和 low-sharing，共 6 cells。
- Autoscaled 的 Baseline B/Candidate B 只运行同一个 sustained-burst trace，共 2 cells。
- Constant workload 仅作预检 smoke，short-burst 仅作机制/负向 smoke，均不进入正式效果数字。

Day 1 还要冻结单 run 最大时长和总 GPU-hour 上限；若预算先耗尽，则整组 cell 标为 incomplete，不选择性删除重复或把 smoke 补成正式结果。

| 配置 | 路由 | 扩缩容 | 用途 |
|---|---|---|---|
| Baseline A | GAIE/llm-d load-aware frozen baseline | Fixed-2 | 已就绪容量与路由基线 |
| Baseline B | 相同 baseline | Week 20 选定 HPA 或 KEDA | 隔离扩缩容整体效果 |
| Candidate A | Week 18 选定 cache-aware 配置 | Fixed-2 | 隔离路由整体效果 |
| Candidate B | 相同 candidate | 与 Baseline B 同一 autoscaler config | 最终组合，不另调参数 |

正式 workloads 包含 mixed、sustained burst、shared-prefix Zipf 和 low-sharing 负向对照；constant/short-burst 只用于上述 bounded smoke。Prefix families 与 arrival seeds 均 held out。只有主假设需要且仍在预设 8-cell/预算上限内时才替换一个 cell 为消融，不能额外扩张全因子 sweep。

报告 overall 及 short/long/prefix-family 分组的 TTFT/TPOT、SLO attainment、goodput、errors/timeouts、actual reused tokens、route/EPP overhead、desired/current/Ready replicas 和 allocated/billed cost。若 Candidate B 无收益或回归，结论照实保留。

## Fault Campaign

每个故障先定义 steady-state checks、注入动作、blast radius、预期 signals、恢复动作与最大时长；一次只注入一个 fault。

| 故障 | 主要验证 | 不允许的误判 |
|---|---|---|
| 一个 vLLM Pod NotReady/terminated | endpoint removal、既有 stream、新请求、cache cold restart | 只看 Deployment available 就称无影响 |
| Gateway data plane/controller 有界重启 | 既有 stream 与新请求分别表现、Route status、alert、重连与恢复 | controller 重启等同 data-plane 中断，或新请求恢复就忽略旧 stream |
| llm-d Router/EPP unavailable/slow | ext_proc failure mode、timeout、fail-open/close、报警和恢复 | HTTP 200 就称路由正确 |
| Prometheus/metric query missing/stale | HPA/KEDA fallback、避免误缩容、alert | empty metric 当零 demand |
| Candidate image readiness failure | rollout pause、canary SLO、rollback 到旧 digest | Pod Running 当模型 ready |

记录客户端、Gateway、EPP、controller、Pod 与 alert 时间线。恢复必须以新请求成功、streaming 正常、route attribution、replica/metric 稳定和 SLO 回归为准，而不只看资源 condition。

## Rollout Contract

1. **Preflight**：server-side dry-run、schema/status、image digest、readiness、route ownership 和 unique writer 全部通过。
2. **Zero-traffic candidate**：部署新 revision，完成 model-load/readiness、direct smoke 与 metrics/trace 标签检查。
3. **Optional mirror**：仅对无副作用合成请求，并确认 provider 支持、mirrored response 被丢弃、容量/计费单列。
4. **Canary**：先从 owner references、managed fields 与 Week 19 contract 确定 route owner；由 KServe 管理时只修改 `LLMInferenceService` owner 字段，由外部 HTTPRoute 管理时只修改该 HTTPRoute，不能同时把两处都当合法 mutation point。随后给 candidate 小权重，按 upstream attempts 核对实际分流，而非要求小样本精确等于权重。
5. **Promote or rollback**：以冻结的 SLO/error/condition 门槛决策；rollback 恢复旧 image/config/route，不能只把 Deployment revision 回退而保留不兼容 CRD/config。
6. **Post-check**：验证长 stream drain、无 orphan resources/双 writer、旧 candidate 流量归零和成本资源回收。

Timeout 必须区分 gateway request、backend request、client 与 autoscaling/rollout deadline。对已输出 token 的 SSE 不做隐藏 retry；mirror/canary 请求不进入正式 held-out 指标，除非在运行前定义为该 cohort。

## Runbook Contract

Runbook 至少包含：

- 服务/模型/版本、owner、dashboard、logs/traces、resource graph 和最近 rollout 链接。
- 以 SLO burn/error、Gateway unavailable/status 异常、无 Ready endpoint、EPP failure、stale metric、scaling stuck 为入口的症状树。
- 只读诊断命令，按 Client → Gateway/Route → InferencePool/EPP → vLLM → autoscaler/metrics 顺序缩小范围。
- 每项缓解动作的前提、blast radius、回滚命令和完成验证；不能把“restart everything”当默认步骤。
- Escalation 条件、证据包、已知 alpha/version-skew 风险及事后复盘模板。
- 每个命令先支持 namespace/context 参数；破坏性操作不使用宽泛 wildcard 或未解析变量。

## 每日安排

| 日期 | 预算 | 任务与产出 |
|---|---:|---|
| Day 1 | 1.5 h | 冻结 release/run manifest、SLO、held-out split、矩阵、预算与 rollback gates |
| Day 2 | 2 h | 运行 Fixed-2 baseline/candidate 的 mixed、shared-prefix 与 low-sharing 六个正式 cells（含预设重复） |
| Day 3 | 2 h | 运行 autoscaled baseline/candidate 的 sustained-burst 两个正式 cells（含预设重复）；constant/short-burst 只作 smoke |
| Day 4 | 1.5 h | 注入 Pod、Gateway 与 EPP/router 故障，验证 alerts、streaming、恢复和 cache cold path |
| Day 5 | 1.5 h | 注入 metric/rollout 故障，演练 canary promotion 与完整 rollback |
| Day 6 | 1.5 h | 计算 SLO/goodput/cost 与不确定性，撰写并 tabletop 验证 runbook |
| Day 7 | 1 h | 冻结最终报告/复现清单、同步证据并清理全部计费资源 |

## 报告必须回答的问题

1. 哪些参数在 held-out 前冻结，如何证明 seeds/prefix families 未参与调优？
2. 路由、扩缩容及组合相对各自 baseline 的收益/回归是什么，哪些 workload 才成立？
3. SLO/goodput 的分子、分母、窗口、错误、超时和 drain 规则是什么？
4. 统计区间以什么独立单位计算，样本量允许和不允许得出哪些结论？
5. Pod、Gateway、EPP、metric 和 bad rollout 故障各自如何被发现、缓解、回滚并验证恢复？
6. Canary 实际 upstream 分流和客户端样本是否符合预期，mirror 带来什么容量/副作用边界？
7. Allocated 与 billed GPU/node-hours 是否同向变化，最终成本结论覆盖哪些资源？
8. 哪些结果只适用于当前 GKE/固定 release，哪些 portable contract 尚未在其他云验证？

## 完成标准

- [ ] Release/run manifest、SLO、held-out split、矩阵、重复数和停止条件在执行前冻结。
- [ ] 八个正式 cells / 24 个 runs 均保留错误、超时、原始数据和 run order；未挑选最好结果，预算中断则整组标记 incomplete。
- [ ] 路由、扩缩容与组合结论分别有对应 baseline，负向 workload 与无收益结果未删除。
- [ ] Pod、Gateway data plane/controller、EPP/router、metric 和 rollout 故障有受控注入、signals、恢复与 SLO 验证证据。
- [ ] Canary/rollback 覆盖 image、config、route 和 unique writer；streaming/drain 无隐藏 retry。
- [ ] Runbook 经一次 tabletop/clean-environment 演练，命令、权限、context、rollback 和 escalation 可执行。
- [ ] SLO dashboard/alerts、统计限制、allocated/billed cost 与跨云外推边界均写清。
- [ ] 所有结论绑定 commit/images/CRDs/results；GPU、node、LB、disk/IP 和临时 control resources 已清理或登记。
