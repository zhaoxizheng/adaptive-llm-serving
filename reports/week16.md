# Week 16 实验报告：Gateway API v1 L7

状态：**not_executed**。入口：[代码导读](../docs/week-16-code-walkthrough.md)、
[验收 contract](../docs/gateway-api-contract.md)。

## 固定版本与能力矩阵

待填：Kubernetes、Gateway API CRD bundle、controller/data-plane release/image digest、
GatewayClass controllerName、conformance evidence、私有入口配置、vLLM/model/replica shape。
逐项填写 contract 中 capability 状态，不能从 core v1 名称推断完整支持。

## 控制面与请求证据

待填：generation/observedGeneration、GatewayClass/Gateway/HTTPRoute/parent conditions、
Pod/Service/EndpointSlice 快照、完整 access-log 窗口与 request→route→backend→Pod 关联。

| Cell | HTTP/stream 结果 | backend attempts/归属 | 判断 |
|---|---|---|---|
| direct / 100-0 | pending | pending | pending |
| 50-50 / 90-10（每次至少 1000 样本、三个重复） | pending | pending | pending |
| path/header match | pending | pending | pending |
| 无匹配 hostname/path | pending | 零命中必须有完整窗口佐证 | pending |
| invalid Service / wrong port / unready endpoint | pending | 分开解释 status 与 runtime | pending |
| long SSE / cancellation | pending | 同 upstream / abort / retry | pending |

## 性能与实现边界

待填：gateway 增量延迟、client TTFT/TPOT、失败、逐 backend request/token、CPU/memory；
确认客户端/代理未先饱和。描述权重统计区间、样本不足、retry 与归属缺失。
区分规范意图、固定 release 的支持、此次实验结果，不泛化到所有 controller。

## Week 17 handoff 与验收

- [ ] 两个固定 Ready replicas 满足 G/TP/rank 约束，无 HPA。
- [ ] 正常与负例 conditions、attribution、SSE/取消和三次重复完成。
- [ ] 实验参数与原始结果已冻结，仍以 core v1 baseline 交接。
- [ ] Git identity、结果同步、GPU/LB/磁盘/IP 残余计费检查完成。
