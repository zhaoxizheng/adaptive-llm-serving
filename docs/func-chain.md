week01 - KV Cache on/off

  configs/week01.yaml
          ↓
  make run-week01
          ↓
  scripts/run_week01.sh
          ├─ prepare_week01_model.py
          ├─ check_env.py
          ├─ src/generate.py
          ├─ src/benchmark_kv_cache.py
          │      └─ src/inference.py
          │             └─ src/latency.py
          ├─ src/analyze_week01.py
          └─ write_week01_status.py
          ↓
  人工完成 reports/week01.md
          ↓
  scripts/verify_week01.py
          ↓
  verification-receipt.json

week02 - 固定 use_cache=true, Batch 推理

 configs/week02.yaml
          ↓
  make run-week02
          ↓
  scripts/run_week02.sh
          ├─ dependency freeze
          ├─ prepare_week01_model.py      # 复用 Week 1
          ├─ check_env.py                 # 复用 Week 1
          ├─ benchmark_batch.py
          │    ├─ week02_contract.py
          │    ├─ week02_smoke.py
          │    ├─ batch_inference.py
          │    ├─ hf_batch_backend.py
          │    ├─ kv_cache_estimator.py
          │    └─ result_store.py
          ├─ analyze_week02.py
          └─ status: artifacts_ready
                  ↓
          完成 reports/week02.md
                  ↓
          verify_week02.py

week03 - 承接 Week 2 的固定 Batch 性能数据，但新增了请求到达时间、排队、调度策略和 overload。
Week 2 正式 Batch 结果
          ↓
  week03_calibration.py
          ↓
  calibration.json：实测 capacity_rps
          ↓
  configs/week03.yaml
          ↓
  prepare_week03_run.py
          ↓
  scripts/run_week03.sh
          ↓
  serve_week03.py
     ├─ workload.py              # 预生成请求 trace
     ├─ dynamic_batcher.py       # admission + batching
     ├─ FakeBatchBackend         # Mac 离散事件模拟
     └─ HFBatchBackend           # L4 真实 GPU 推理
          ↓
  events.csv + batches.csv
          ↓
  analyze_week03.py
          ↓
  reports/week03.md
          ↓
  verify_week03.py

week04 - Week 3 的 dynamic_batcher.py 在这一周由 vLLM 的 Scheduler 和 KV 管理接替。
  configs/week04.yaml
          ↓
  make run-week04
          ↓
  scripts/run_week04.sh
     ├─ vllm_offline_smoke.py
     │      └─ LLM.generate()              # 直接推理 smoke
     │
     ├─ start_vllm.py
     │      └─ vllm_contract.server_argv()
     │             └─ 启动 vllm serve
     │                    └─ /health + /v1/models readiness
     │
     ├─ openai_smoke.py                    # 普通 HTTP / SSE smoke
     │
     ├─ vllm_contract.expand_cases()
     │      └─ benchmark_vllm.sh
     │             └─ vllm bench serve
     │                    └─ POST /v1/completions
     │
     └─ start_vllm.py stop
          ↓
        同步结果、停止 VM

真正的请求调度发生在下面这条链中。仓库固定版本是 vLLM 0.10.2，以下按普通 V1 同步执行主线展开：

  POST /v1/completions
          ↓
  OpenAIServingCompletion.create_completion()
     校验模型、处理 prompt、构造 SamplingParams
          ↓
  AsyncLLM.generate() in async_llm.py
          ↓
  AsyncLLM.add_request()
     └─ Processor.process_inputs()
            └─ EngineCoreRequest
          ↓
  AsyncLLM._add_request()
     ├─ OutputProcessor.add_request()       # 先登记输出接收者
     └─ engine_core.add_request_async()
          ↓
  ZMQ IPC                                  # 进入 Engine Core
          ↓
  EngineCore.add_request()
          ↓
  Scheduler.add_request()
     └─ waiting 队列
          ↓
  反复执行 EngineCore.step()
     │
     ├─ Scheduler.schedule()
     │    ├─ 为 running 请求安排本轮 token
     │    ├─ KVCacheManager.allocate_slots()
     │    ├─ 容量不足时可能 preempt，释放 KV 后回到 waiting
     │    ├─ 有剩余预算和 sequence 槽位时接纳 waiting 请求
     │    └─ 生成 SchedulerOutput
     │
     ├─ model_executor.execute_model(SchedulerOutput)
     │    └─ Worker.execute_model()
     │         └─ GPUModelRunner.execute_model()
     │              ├─ 更新 batch 状态、准备输入和 block table
     │              ├─ model forward
     │              └─ logits → sampling → token IDs
     │
     └─ Scheduler.update_from_output()
          ├─ 更新输出 token 和停止状态
          ├─ 完成请求释放 KV
          └─ EngineCoreOutputs
          ↓
  IPC 返回 API 侧
          ↓
  OutputProcessor.process_outputs()
     └─ detokenize → RequestOutputCollector
          ↓
  AsyncLLM.generate() yield RequestOutput
          ↓
  Serving 层封装 SSE
          ↓
  客户端解析 content / usage / [DONE]