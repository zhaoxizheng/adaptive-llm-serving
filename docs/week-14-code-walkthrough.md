# Week 14 中文代码导读：Chunk Budget、执行模式与单机 TP

代码把实验变量、GPU 数和输入 trace 显式记录下来，方便解释一次配置变化到底影响了谁。
对应 [学习计划](week-14-plan.md)、[参考资料](week-14-references.md)、
[共同运行约定](week-13-16-runbook.md)、[报告模板](../reports/week14.md)。

## 1. 代码地图

| 文件 / 函数 | 本周职责 |
|---|---|
| [week14-optimization.yaml](../configs/week14-optimization.yaml) | 4096/1024/256 token budget、eager 与组合回归 |
| [week14-tp.yaml](../configs/week14-tp.yaml) | 同主机 device 0 与 devices 0/1 的 TP 对照 |
| [serving_experiment.py](../src/serving_experiment.py) `engine_command` | 白名单 engine 参数转 argv，不执行 shell 字符串 |
| [run_serving_experiment.py](../scripts/run_serving_experiment.py) `gpu_identity/run_cell/main` | 固定设备、重复、原始证据和失败退出 |
| [analyze_serving_study.py](../src/analyze_serving_study.py) | 按重复输出 CSV、TTFT 图，保留负收益 |
| [serving-baseline.yaml](../configs/serving-baseline.yaml) | 交接 Week 15 的单副本形态和 SLO |
| [deployment.yaml](../deploy/vllm/single-node-multigpu/deployment.yaml) | 一个 Pod 申请两张 GPU、TP=2 的模板 |
| [cluster_contract.py](../src/cluster_contract.py) `verify_rank_evidence` | 验证实际 Pod/node/rank/GPU 映射 |

## 2. 单变量实验和组合实验

`baseline` 固定 `max_num_batched_tokens=4096`；`chunk_256` 与 `chunk_1024` 只覆盖
该值，保留 max-num-seqs、模型、dtype、APC 等参数。所有候选都使用 short、long、mixed、
shared 四种 workload。mixed 中每四条请求有一条 long，结果按 short/long 分组保存。

每个 repeat 内候选共享 trace；不同 repeat 轮转候选顺序，降低运行次序与温度变化的偏差。
独立重复仍不能消除 GPU clock/其他进程干扰，所以运行时保存 GPU sample 和拓扑。

`combination` 同时修改 budget 和 eager，明确标记为组合回归，不会把它当成单变量因果
证据。它是待测候选，不代表已经选出的最佳参数。

## 3. 为什么 eager 不叫 graph-only

当前默认 A/B 用 `enforce_eager`，可能同时影响 compilation 和 graph。配置给它标记
`execution_mode_bundle_not_graph_only`。请结合 Week 10/12 的实际 dispatch/capture
证据确认路径；默认 false 只表示允许 graph，不证明本次请求发生 replay。

如果固定版本支持单独设置 graph mode，可在冻结的 engine contract 中加入对应结构化
compilation 配置，再保持其他编译参数一致。需要另外保存 capture sizes、padding、
startup 时间、graph memory、KV blocks；未控制 KV capacity 的结果必须披露混杂因素。
本 runner 不把这些 runtime 事实从布尔配置里推导出来。

## 4. TP 的资源检查

`week14-tp.yaml` 让 TP=1 使用 `[0]`，TP=2 使用 `[0,1]`。`run_cell()` 先检查
device 数量、去重后的 device 数量与 TP 相同，再设置 child 的 `CUDA_VISIBLE_DEVICES`。
`gpu_identity()` 保存 hostname、GPU UUID/driver/memory、`nvidia-smi topo -m` 和依赖版本。

同机两卡试验的模型必须同时能跑 TP=1 和 TP=2。双卡不可用时不要修改脚本返回成功，
在报告中标记 deferred。TP=2 使用更多 GPU，其吞吐增长不是同预算收益；同预算的
`1 × TP=2` 对 `2 × TP=1` 要在 Week 15 单独做。

Kubernetes 模板申请 `nvidia.com/gpu: 2`，设置 TP=2、PP=1。Pod 调度到单个 node，
但模板只能证明资源意图。实际部署后还需保存 nodeName、visible GPUs、worker logs 与
rank mapping。验证器接收如下证据，不替你生成“观测到的”数据：

```json
{
  "pod_uid": "actual-pod-uid",
  "node": "actual-node",
  "tensor_parallel_size": 2,
  "ranks": [
    {"local_rank": 0, "gpu_uuid": "GPU-actual-0", "node": "actual-node"},
    {"local_rank": 1, "gpu_uuid": "GPU-actual-1", "node": "actual-node"}
  ]
}
```

`verify_rank_evidence(pod, evidence, 2)` 对齐 Pod UID、node、GPU allocation、rank 集合及
唯一 GPU UUID；出现跨 node rank 或两个 rank 指向同一 GPU 都失败。

```bash
python3.12 -m scripts.verify_replica_shape --pod pod.json --ranks ranks.json \
  --gpus 2 --output results/week14/tp-shape-verification.json
```

## 5. 统计与选择

`summary.json` 给出全部请求和 `by_workload`，失败不会从分母消失。`eligible` 只检查
请求错误率与客户端 arrival lag，它是必要条件，不是最终选择结论。请继续检查各组
TTFT/TPOT SLO、质量、OOM/preemption、显存和重复间波动。

`analyze_serving_study` 保留每个 repeat 的行，不把不同 trace 或不同硬件混成一个均值。
它拒绝显式标记为 profiler 的输入，生成 TTFT 图和 runs.csv。Pareto 选择由报告给出，
不自动覆盖 `serving-baseline.yaml`。

## 6. 命令

```bash
make plan-week14 PYTHON=python3.12
PYTHON=.venv-vllm/bin/python bash scripts/run_week14_optimization.sh \
  --run --rate low --case chunk_1024 --workload mixed
CONFIG=configs/week14-tp.yaml PYTHON=.venv-vllm/bin/python \
  bash scripts/run_week14_optimization.sh --run --rate low
python3.12 -m src.analyze_serving_study --root results/week14 --output results/week14/tables
```

结果保留 `server.log`、serve help、runtime topology、GPU series、逐请求输出摘要和
`allocated_gpu_seconds`。这个成本包含本地 server 启动/预热/测量/清理，VM 空闲费用需补录。

## 7. 冻结下游 baseline

在 [serving-baseline.yaml](../configs/serving-baseline.yaml) 填入已验证的模型/engine
配置、SLO、near-SLO RPS、GPU topology、image digest、`gpus_per_replica=G` 与 `TP=G`。
`selection_evidence` 指向报告和原始结果，最后才把 `frozen` 改成 true。

这个动作表示证据已审核，不是性能自动达标。Week 15/16 的路由只选择完整副本 endpoint，
HPA 只能改变 replica count，不能在 Pod 内改变 G 或 TP。
