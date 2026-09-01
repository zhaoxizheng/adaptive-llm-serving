# Week 12 Plan: 用 Nsight Systems 重建 CPU–GPU Timeline

> 时间预算：10–12 小时
>
> 本周主线：把 Week 11 的 framework-level 结论下钻到系统 timeline。使用 Nsight Systems 和 NVTX 对齐 scheduler、input preparation、CUDA API、kernel、memory copy 与 CUDA Graph replay，判断 GPU idle gap 来自 CPU、同步、通信还是 workload 本身。

## 本周目标

完成本周后，应当能够：

1. 用可控 capture window 收集 CPU、CUDA、NVTX 和 OS runtime timeline。
2. 区分 CUDA API 调用、kernel execution、memcpy、synchronization 与 graph replay。
3. 将 Week 10 NVTX ranges、Week 11 operator 与 Nsight timeline 对齐。
4. 量化 prefill、decode 和 mixed workload 中的 GPU busy/idle 区间。
5. 判断 decode gap 是 host launch、显式同步、数据准备还是 graph dispatch 导致。
6. 选出 Week 13 应由 Nsight Compute 深挖的一个具体 kernel 或 kernel family。

## 本周边界

- 固定 Week 11 的模型、revision、operating point 和 workload trace。
- Nsight run 与无 profiler baseline 分开，不用 instrumented latency 更新容量结论。
- 只使用 Nsight Systems 做系统级 timeline；不在本周学习 Nsight Compute 的 kernel metrics。
- 不全程 capture server，不启用与问题无关的 trace domains。
- 单 GPU 主线不引入 tensor parallel 或网络通信。
- Prometheus/Grafana 只用于确认 run 健康，不重复学习。

## 本周最终产出

- `configs/week12-nsys.yaml`：capture domains、window 与 workload
- `scripts/run_week12_nsys.sh`：baseline、warmup、capture 和 export
- `results/week12/nsys/`：`.nsys-rep` 与导出统计
- `results/week12/tables/`：CUDA API、kernel、memcpy 和 NVTX 摘要
- `results/week12/figures/`：prefill/decode/mixed 的标注 timeline
- `reports/week12.md`：GPU gap RCA、证据限制和 Week 13 handoff

## Timeline Contract

每个 capture 至少能够定位以下范围：

```text
request arrival
  └─ scheduler step
      └─ prepare inputs
          ├─ H2D / tensor update
          └─ model execute
              ├─ CUDA API launches or graph replay
              ├─ GPU kernels
              └─ D2H / sample result
                  └─ engine output
```

每个结论必须说明使用哪个时钟和区间。不要将 NVTX CPU range、CUDA API duration 和 GPU kernel duration 混成同一指标。

## 四个 Timeline 场景

### Scenario A：Long prefill

- 复用 Week 11 的 long-prefill trace。
- 目标：识别大计算区间、kernel composition、memory copy 与 GPU occupancy pattern。

### Scenario B：Eager steady decode

- 强制 eager，capture 稳态 decode steps。
- 目标：量化 CPU launch sequence、kernel 间 gap 和同步。

### Scenario C：CUDA Graph steady decode

- 使用 Week 10 已验证的 graph replay 配置。
- 目标：与 B 比较 launch pattern 与 GPU idle gap；保持其他变量不变。

### Scenario D：Mixed chunked prefill + decode

- 复用固定到达序列和 scheduler trace。
- 目标：观察 long prefill chunks 如何改变 decode 的 GPU timeline 与 client TPOT。

## Gap 分类

对 GPU idle interval 使用以下分类，不用笼统的“GPU 没吃满”：

| 类型 | 需要的证据 |
|---|---|
| Host launch gap | CPU thread 正在逐个提交 CUDA work，GPU queue 暂时为空 |
| Synchronization | CUDA sync API 或 blocking copy 与 gap 对齐 |
| Input preparation | NVTX prepare range 占据 gap，尚未提交 GPU work |
| Graph dispatch | graph launch/replay 前后的 CPU 与 GPU event 可见 |
| Workload bubble | scheduler 没有足够 runnable tokens，请求/队列证据一致 |
| Unknown | 当前 trace domains 无法支持归因，明确列出缺失证据 |

## 每日安排

### Day 1：Nsight Systems 最小 capture（约 1.5 小时）

- [ ] 阅读 CLI capture、delay/duration 与 export 语义
- [ ] 记录 nsys、driver 和 CUDA 版本
- [ ] 用极短 workload 验证 `.nsys-rep` 可打开和导出
- [ ] 只启用 CUDA、NVTX 与必要 OS runtime
- [ ] 测量 capture 对 wall time 的影响

### Day 2：NVTX 与跨层对齐（约 1.5–2 小时）

- [ ] 复用 Week 10 ranges，不重复添加高频日志
- [ ] 为 capture window 添加明确 start/stop marker
- [ ] 对齐 scheduler step、prepare、execute 和 sample
- [ ] 确认 range 位于预期线程和进程
- [ ] 保存最小 timeline 注释规范

### Day 3：Long prefill（约 1.5–2 个计费小时）

- [ ] 运行 Scenario A baseline/capture pair
- [ ] 导出 CUDA GPU kernel 与 API summary
- [ ] 标注 H2D/D2H、compute 和 idle intervals
- [ ] 与 Week 11 long-prefill operators 对照
- [ ] 记录值得 Week 13 深挖的 kernel family

### Day 4：Eager decode（约 1.5–2 个计费小时）

- [ ] 运行 Scenario B
- [ ] 统计每步 launches、kernel duration 与 inter-kernel gap
- [ ] 找出 blocking/sync API
- [ ] 区分 input preparation 和 launch overhead
- [ ] 对照无 capture TPOT

### Day 5：CUDA Graph A/B（约 1.5–2 个计费小时）

- [ ] 运行 Scenario C
- [ ] 验证 graph replay event 和 capture size
- [ ] 与 eager 使用相同 steady decode shapes
- [ ] 比较 CPU API pattern、GPU gap 与 baseline TPOT
- [ ] 不把不同 batch shape 混入 graph A/B

### Day 6：Mixed workload 与 gap RCA（约 1.5–2 个计费小时）

- [ ] 运行 Scenario D
- [ ] 将 long-prefill chunk 与 decode delay 对齐
- [ ] 用 scheduler trace 排除 workload bubble
- [ ] 给主要 idle gaps 分类并标注证据
- [ ] 同步所有结果后停止 VM

### Day 7：报告与 Week 13 handoff（约 1.5 小时）

- [ ] 生成三张裁剪且带标注的 timeline
- [ ] 保存 `.nsys-rep`、CLI 参数和导出表
- [ ] 对 Week 11 结论逐项确认、修正或保留未知
- [ ] 选择一个 kernel 或 kernel family 交给 Week 13
- [ ] 写明为何选择它及需要的 Nsight Compute metrics

## 报告必须回答的问题

1. Prefill 与 decode 的 GPU timeline 结构有何不同？
2. Eager decode 的主要 gaps 出现在何处，由什么证据归因？
3. CUDA Graph replay 改变了哪些 CPU API 与 GPU gap？
4. 是否存在 blocking copy 或 synchronization？它影响哪个阶段？
5. Mixed workload 的 decode delay 来自同一 GPU step 变长，还是 scheduler 等待？
6. Profiler overhead 是否改变了 batching 或 execution mode？
7. Week 13 最值得深挖哪个 kernel，为什么？

## 本周不要做什么

- 不 capture 数分钟完整压测并期待人工浏览。
- 不把 API row 与 GPU kernel row 的 duration 直接相加。
- 不看到 GPU idle 就直接归因 CPU bottleneck。
- 不比较不同 shape 的 eager 与 graph trace。
- 不只保存截图而丢弃 `.nsys-rep`、CLI 和环境信息。
- 不在本周跳入 Nsight Compute 的大量 counter。

## 完成标准

- [ ] 四个场景均有 baseline/capture pair
- [ ] Scheduler、NVTX、CUDA API 与 GPU kernel 能在 timeline 对齐
- [ ] 主要 GPU gaps 均有分类或明确标为 unknown
- [ ] Eager 与 CUDA Graph A/B 控制了 workload shape
- [ ] Week 11 的三个假设已被系统 timeline 复核
- [ ] 原始 `.nsys-rep`、export、配置与脚本齐全
- [ ] Week 13 kernel target 和所需证据已确定
- [ ] GCP VM 已停止，结果已同步并绑定 Git commit
