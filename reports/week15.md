# Week 15 实验报告：RR、多副本与生命周期

状态：**not_executed**。入口：[代码导读](../docs/week-15-code-walkthrough.md)。

## 环境与资源形态

待填：独立 context/namespace、image digests、模型 revision、G/TP、Pod UID/node/ranks、
GPU quota、CPU/memory、网络入口、cache/镜像下载状态、Git identity。
总 GPU = replica count × G；同预算 TP 对照单独记录。

## 路由与服务结果

待填：四种 workload 的 single/RR/HPA-burst cells，至少三个重复、trace identity、
请求/attempt/Pod 归属、RR epoch/sequence、逐副本 request/token/queue/cache、
TTFT/TPOT/goodput、失败/超时、代理与 load generator 资源和延迟。

## 生命周期

| 时间线 | 证据 |
|---|---|
| schedule → image pull → model load → Ready → admission → first token | pending |
| HPA metric → desired/current → Ready → first token | pending |
| endpoint removal → gateway refresh → drain → process exit | pending |
| 强制 Pod 失败 → 中断 stream → 后续请求恢复 | pending |

## 结论与成本

待回答：请求数均衡为何可能不等于 token/GPU load 均衡？CPU HPA 是否实际触发？
增益对应多少 GPU-seconds/hours？SSE 是否保持同一 upstream、取消是否到达 engine？
正常 drain 与失败注入分别丢失哪些请求？哪个 workload 交给 Week 16/18？

- [ ] RR/SSE/取消有 runtime 证据，CPU 单测仅作为代码验证。
- [ ] 每个 replica 的 GPU/TP/rank 形态已验证。
- [ ] 四类 workload、HPA/固定基线、cold-start/drain/failure 原始结果齐全。
- [ ] 初始 cache/replica 状态和 HPA 非同预算限制已披露。
- [ ] 输出同步，GPU、磁盘、LB、公网 IP 账单与清理状态已记录。
