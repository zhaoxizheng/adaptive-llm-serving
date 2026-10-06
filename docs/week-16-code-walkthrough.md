# Week 16 中文代码导读：Gateway API v1 L7 Baseline

本周使用标准 Gateway API 资源描述 L7 行为，再用条件、请求归属和失败结果验证具体
controller 的实现。对应 [学习计划](week-16-plan.md)、[参考资料](week-16-references.md)、
[Gateway contract](gateway-api-contract.md)、[报告模板](../reports/week16.md)。

## 1. 代码地图

| 文件 / 函数 | 作用 |
|---|---|
| [baseline.yaml](../deploy/gateway-api/baseline.yaml) | GatewayClass/Gateway/HTTPRoute 和两个独立 backend Services |
| [week16-l7-matrix.yaml](../configs/week16-l7-matrix.yaml) | 直连、100/0、50/50、90/10、match 与失败 cases |
| [cluster-lab.yaml](../configs/cluster-lab.yaml) | CRD/controller/data-plane/Kubernetes 版本和 private 策略锁 |
| [cluster_contract.py](../src/cluster_contract.py) `gateway_resources/negative_route` | 从统一 baseline 渲染资源 |
| 同文件 `current_conditions/weight_verdict` | generation/parent 条件与预先固定的权重统计判断 |
| [run_cluster_study.py](../scripts/run_cluster_study.py) `status_gate/benchmark` | 运行前检查、HTTP Host/header、原始结果与快照 |
| [analyze_routing.py](../src/analyze_routing.py) `attribution` | 客户端请求与 backend attempts 的关联 |

## 2. 四层对象如何连接

```text
GatewayClass: serving-lab       controllerName 固定实现
  Gateway: inference           HTTP listener / inference.lab.invalid
    HTTPRoute: inference       exact path + X-Lab-Case
      Service: vllm-a/vllm-b    selector 指向不同 Deployment
        Pod identity proxy     输出真实 Pod UID
          单节点 vLLM engine  G GPUs / TP=G
```

两个 Service 各自只选择一个独立 replica；权重分配的单位是 Service backendRef。
如果让两个 Service 选择同一组 Pods，就无法解释 A/B attribution，模板避免了这种重叠。
所有 route/backend 在同一 namespace，不引入跨 namespace ReferenceGrant。

## 3. 模板与已验证版本分开

`--render` 可以在 Mac 输出模板；里面的 `REPLACE_WITH_*_IMAGE_DIGEST` 不能直接用作
实验版本。真正的 `--apply` 要求 frozen serving baseline、镜像 digest、明确 lab context、
私有入口策略，以及 CRD/controller/data-plane release/capability evidence。

代码不安装 Gateway API 或 controller。`controller_name` 的默认值只是模板选择，不能
代替已安装实现。私有 data-plane Service 往往依赖 controller-specific 配置，本周应先
固定并审查它；Gateway API core 本身不保证自动生成的 listener 一定是私有入口。

## 4. `current_conditions()` 为什么检查 generation

资源 spec 更新后，旧的 `Accepted=True` 可能仍在 status 中。函数要求条件的
`observedGeneration` 与当前 metadata.generation 一致。

- GatewayClass：Accepted。
- Gateway：Accepted 与 Programmed。
- HTTPRoute：指向本 namespace 的指定 Gateway parent，Accepted 与 ResolvedRefs。

默认 runtime gate 还要求 vllm-a/vllm-b 各一个 Ready replica，并拒绝本实验 HPA。
这些检查只能证明当前配置已被控制面接受，仍需流量验证；失败 cell 的 negative route
可能预期 ResolvedRefs=False，应保留它的独立 status，不用它替代正常主 route 的 gate。

## 5. 核心代码精读

### Ready 必须属于当前 spec，也必须属于正确 parent

`current_conditions()` 是本周等待控制面收敛的关键判据：

源码：[src/cluster_contract.py](../src/cluster_contract.py)，第 146–158 行；以下为原文摘录，仅移除公共缩进。

```python
def current_conditions(resource, required, *, parent=None):
    generation = resource["metadata"]["generation"]
    if parent is None:
        conditions = resource.get("status", {}).get("conditions", [])
    else:
        matches = [p for p in resource.get("status", {}).get("parents", [])
                   if p.get("parentRef", {}).get("name") == parent
                   and p.get("parentRef", {}).get("namespace", resource["metadata"].get("namespace")) == resource["metadata"].get("namespace")]
        if len(matches) != 1:
            return False
        conditions = matches[0].get("conditions", [])
    return all(any(c["type"] == kind and c["status"] == "True" and c.get("observedGeneration") == generation
                   for c in conditions) for kind in required)
```

先读取对象当前 `metadata.generation`。Gateway 直接检查顶层 conditions；HTTPRoute
指定 parent 时，从 `status.parents` 找到该 Gateway 对应的记录，缺失或有歧义返回 False。
最后的两层量词是“每个 required kind，都存在一个 True 且 generation 匹配的 condition”。

例：HTTPRoute 已从 generation=7 改到 8，而 status 仍是 `Accepted=True,
observedGeneration=7`。只检查 True 会在新路由尚未被处理时开始压测；该函数拒绝旧
generation，等待 controller 对新 spec 给出条件。Accepted 与 ResolvedRefs 分别回答
接受附着和引用解析，二者不能互相代替。

**设计取舍与边界。** 本 helper 适配同 namespace、单一明确 parent 的实验模型。
它并没有完整比较 `controllerName`、`sectionName` 等所有 parent identity 字段；
扩展到多 listener、多 controller 或跨 namespace 前必须扩充匹配规则。
即使条件全部通过，也要沿 listener → HTTPRoute match → backendRef → Ready endpoint
跑 generation/streaming smoke；status 不能证明实际流量命中了预期 rule。

再对照 `gateway_resources()`：同一 match 内 path 与 header 是 AND，不同 matches
为候选匹配项；backendRefs 的 weight 是相对比例，不是严格轮转顺序。90/10 不能套用
Week 15 的 A/B sequence 验证，更不能从 client header 直接伪造实际 route attribution。

**读后自检。** 刚修改 backendRef 后哪些旧 conditions 会看似正常？如果一个 Route
附着两个 parents，为什么不能只取 `status.parents[0]`？

## 6. 互斥规则如何避免歧义

正常规则都匹配 exact `/v1/completions`，再匹配不同的 `X-Lab-Case`：

| Header | 权重 A/B | 目的 |
|---|---|---|
| only-a | 100/0 | 单路径 smoke |
| half | 50/50 | 均匀权重 |
| canary | 90/10 | 小流量 backend |
| match-a / match-b | 100/0 或 0/100 | Header 归属 |

错误 Host 与错误 path 不应命中这些规则。`invalid_backend` 引用不存在的 Service，
`wrong_port` 引用已存在 Service 的错误 port，两者都保存具体 condition 和 HTTP status。
未 Ready endpoint 还需在真实集群另外控制 backend readiness；不能把 invalid Service
实验当成同一种失败。判断“无后端命中”时需要完整的 gateway/identity 日志窗口，HTTP
错误本身不足以证明请求没有到达模型。

## 7. 权重为何不要求精确 900/100

`weight_verdict()` 固定使用至少 1000 个请求和 99.9% Wilson interval。预期比例在区间内
记为 compatible，小样本记为 insufficient_samples。99.9% 是本仓库预先选择的实验规则，
不是 Gateway API 规范规定的阈值，也不是多个重复的自动多重检验校正。

100/0 与 0/100 的零权重必须严格检查；不能用统计容差原谅零权重 backend 收到请求。
真实 weighted backend counts 不保证按周期轮转，所以不用 Week 15 的 RR 序列规则验证。

每个请求的 upstream attempts 单独计数。缺失归属、出现 retry 或未知 backend 时，分析
标为 `incomplete_or_retried_attribution`，不把最终成功 backend 当成全部选择历史。
request 数比例与 token workload 比例也分别分析。

## 8. Runtime attribution 的边界

identity proxy 给出 request ID、backend Pod UID 与本跳 attempt；它看不到 controller
实际选中的 HTTPRoute/rule。`analyze_routing` 会报告 `route_attribution_missing`，不会
从客户端 X-Lab-Case 自动编造一个“观测到的 route”。

请把固定 controller access logs 按 [contract](gateway-api-contract.md) 归一化成 JSONL，
保留 route_name/backend Service/Pod UID/attempt。分析时只输入同一层日志，避免把 gateway
和 identity 两跳当成两次 retry。正式性能测量关闭高频 debug 后仍应保留最小 attribution。

## 9. 命令

```bash
make plan-week16 PYTHON=python3.12
bash scripts/run_week16_gateway.sh --render --output /tmp/week16-template.yaml
# 固定版本锁、清理 Week 15 实验 HPA/旧 replicas 后：
bash scripts/run_week16_gateway.sh --preflight
bash scripts/run_week16_gateway.sh --server-dry-run --case only_a
bash scripts/run_week16_gateway.sh --apply --case only_a
# endpoint 指向实际 private listener；低负载 smoke 可显式 port-forward。
bash scripts/run_week16_gateway.sh --run --case half --cache-state cold --cancel-smoke
bash scripts/run_week16_gateway.sh --apply --case invalid_backend
bash scripts/run_week16_gateway.sh --run --case invalid_backend --cache-state mixed
python3.12 -m src.analyze_routing \
  --clients results/week16/SESSION/r0-short/client.jsonl \
  --attempts results/week16/SESSION/controller-attempts.jsonl \
  --weights 50 50 --output results/week16/analysis
```

`--run` 会跑选中 case 的三个重复。交错 B/C/D 时，按配置复制成一次一组的运行安排，
保留每组独立 repeat；默认入口不会自动在多个 controller 路由之间切换或创建外部入口。
直连 case 默认使用 localhost:8082，先单独 port-forward backend Service。

## 10. 交给 Week 17

交接固定 CRD/controller release、主路由 manifests、双副本 shape、workload、SLO、
连接路径、attribution schema、失败语义和原始结果。本周不引入 InferencePool/EPP/llm-d，
后续比较才能把 endpoint selection 的变化与 L7 基线分开归因。
