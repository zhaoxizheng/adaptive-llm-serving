# vLLM V1 Scheduler Map

固定 `v0.10.2 / 01efc7ef781391e744ed08c3292817a773d654e6`。
本文件记录源码规则；实际行为需要与 [Week 8](week-08-code-walkthrough.md) 的 trace 对照。

| 入口 / 数据 | 固定源码 | 需要读的内容 |
|---|---|---|
| `EngineCore.step` | [core.py](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/v1/engine/core.py) | schedule → executor → update_from_output 的调用顺序 |
| `Scheduler.__init__`、`schedule` | [scheduler.py](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/v1/core/sched/scheduler.py) | `max_num_running_reqs`、`max_num_scheduled_tokens`、running/waiting collections |
| `SchedulerOutput` | [output.py](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/v1/core/sched/output.py) | new/cached requests、scheduled token map、finished IDs 与 block 信息 |
| `Request`、`RequestStatus` | [request.py](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/v1/request.py) | WAITING、RUNNING、PREEMPTED、FINISHED_*，prompt/computed/output tokens |
| `KVCacheManager.allocate_slots`、`free` | [kv_cache_manager.py](https://github.com/vllm-project/vllm/blob/01efc7ef781391e744ed08c3292817a773d654e6/vllm/v1/core/kv_cache_manager.py) | scheduler 与 block manager 的调用接口；内部算法留到 Week 9 |

## 普通 text-only、单 GPU 路径

1. `add_request()` 把请求加入 waiting，并放入 request 字典。
2. `schedule()` 首先遍历 running，按“需要补齐的 token 数”和剩余 token budget 确定
   本步工作量，再尝试分配 KV slots。
3. running 的 allocation 失败时，根据配置 policy 选择 preempt 对象，释放其 cache，
   设置 PREEMPTED，computed tokens 清零，放回 waiting 前部。默认 FCFS 分支与 priority
   分支的选择方法不同，不用函数名推断所有 policy 行为相同。
4. 当本步没有 preempt 等限制时，继续尝试从 waiting admit；sequence limit、token
   budget、KV allocation 等都可能让 admission 停止。
5. `SchedulerOutput` 固化已经产生的 decision；随后 `_update_after_schedule()` 更新
   computed token 计数。
6. `update_from_output()` 消费模型结果，更新输出 token 和 stop 状态。完成请求经
   `_free_request()`、`_free_blocks()` 释放。外部 abort 经过 `finish_requests()`。

三种约束不能互换：`max_num_seqs` 限制 running request 数，token budget 限制一轮工作量，
KV capacity 限制这些请求能否同时驻留所需历史。GPU utilization 高低不能替代这三个条件。

## Trace 的解释边界

Step event 的 computed tokens 取自更新前；KV usage 是该次 decision 后的占用快照。
preemption 会清零 computed tokens，因此恢复后的 prompt 可能再次占用 token budget。
某个请求出现多个 prefill step 既可能因为 chunking，也可能涉及重新计算，要结合
preempted 事件解释。

本实验禁用 prefix caching，采用普通 text requests。源码中还有 encoder、LoRA、FSM、
speculative decoding、KV connector 和 async scheduling 分支；这些分支不因 schema
省略了字段就不存在。当前 parser 遇到非主线 state 时拒绝解析，避免把它简化为普通
waiting/running 后得出错误结论。

交给 Week 9 的问题：block pool 如何决定可用 blocks；free 后何时可复用；prefix hash
与 cache hit 如何改变 computed tokens；eviction 与 scheduler preemption 的责任边界。
