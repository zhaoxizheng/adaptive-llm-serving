# 第 9–12 周共同运行约定

四周继续使用单张 NVIDIA L4、Week 6 operating point 和 vLLM `v0.10.2`：
`01efc7ef781391e744ed08c3292817a773d654e6`。这里的代码和 CPU 测试可以在 Mac 阅读、运行；
真实 KV trace、CUDA execution、PyTorch CUDA profile 和 Nsight capture 必须在 GPU VM 上采集。
仓库中的报告是待填写模板，不能作为已经完成实验的证明。

## 先检查前置条件

1. Week 6 的 `results/week06/analysis.json` 已产生 `operating_point` 或明确的 `fallback`。
   Runner 直接复用其中的启动参数，再添加场景写明的 override。没有该文件时，`--plan`
   使用 Week 4 配置展示命令；`--run` 会拒绝继续。
2. 使用 Week 7 的源码环境准备方法，使 `.venv-vllm/bin/python` 真正 import
   `vendor/vllm/vllm`。只在源码目录打 patch、仍运行另一份 wheel 会被拒绝。
   参考 [Week 7 导读](week-07-code-walkthrough.md) 的源码准备说明。
3. 保持普通文本、单 KV full-attention group、同步调度、单 GPU。不能把 speculative、
   LoRA、KV transfer、异步调度或多卡配置混入这些解析器的支持范围。
4. Week 12 需要 VM 已安装可用的 `nsys` CLI。脚本检查 capture-range 和 fork 选项，
   保存实际版本与 help；它不自动安装工具。

不需要先启动服务。每个场景由 runner 在空闲的 loopback 端口启动自己的服务，并在退出时
清理自己的进程组。端口已占用就停止，不接管已有服务。

## Patch 应用顺序

从 Week 8 已完成的源码 checkout 开始，第 9 周仅新增：

```bash
git -C vendor/vllm apply --check ../../patches/week09-kv-trace.patch
git -C vendor/vllm apply ../../patches/week09-kv-trace.patch
make run-week09 VLLM_PYTHON=.venv-vllm/bin/python
```

第 10 周开始再应用执行 patch，第 11–12 周继续使用同一份源码：

```bash
git -C vendor/vllm apply --check ../../patches/week10-execution-trace.patch
git -C vendor/vllm apply ../../patches/week10-execution-trace.patch
make run-week10 VLLM_PYTHON=.venv-vllm/bin/python
```

完整顺序是 Week 7 → 8 → 9 → 10。第一次准备 checkout 时先按 Week 7/8 导读完成前两份
patch；不要把 Week 9 patch 直接应用到原版 wheel。`verify_source()` 会核对固定 HEAD、
所有相关文件的字节内容、额外源码修改和实际 import 路径。

Week 9 的验证期望停留在 Week 9 patch 状态。已进入 Week 10 后重做 Week 9，可以使用
另一份停留在 Week 9 的独立 checkout，并在配置中修改 `source_checkout` 所在的 Week 7
配置路径及对应 Python 环境；不要对含有自己修改的源码执行强制 reset。

重建 patch 而不覆盖仓库文件：

```bash
python3.12 -m scripts.build_deep_trace_patches \
  --source vendor/vllm --output /tmp/rebuilt-deep-patches
```

Builder 用 `git show <固定 commit>:<文件>`，因此 checkout 已应用 patch 也能重建。
它通过唯一 anchor 添加 hook，再做 Python 编译检查（包含 future-import 顺序）；
不会修改 scheduler 或 eviction 算法。

## 四周入口

```bash
# Mac：验证配置并打印真实命令；不加载 torch/vLLM，不产生 GPU 结果。
make plan-week09 plan-week10 plan-week11 plan-week12 PYTHON=python3.12

# L4 VM：满足前置条件后执行；每周也可以只跑单个场景。
make run-week09
make run-week10
make run-week11
make run-week12
```

Makefile 的 `VLLM_PYTHON` 默认是 `.venv-vllm/bin/python`，`PYTHON` 控制 CPU 预演。
Shell 入口用 `PYTHON` 选择解释器，例如：

```bash
PYTHON=.venv-vllm/bin/python bash scripts/run_week11_profiler.sh --run --scenario decode
```

## 从配置到结果的共同调用链

```text
load_config → selected_command → override_command → verify_source
    → make_jobs → managed_server → send_jobs
    → parser / profiler export → observation.json → run.json
```

`make_jobs()` 从固定 tokenizer 的普通 token ID 集合中生成合成输入。seed、family 和
长度决定 token 序列；相同场景的 baseline/profile 输入相同，Week 11/12 共享
[profiling-workloads.yaml](../configs/profiling-workloads.yaml)。这是机制实验输入，不是
生产语料；不要用其速度推断真实业务容量。

`run.json` 保存配置、fingerprint、源码 fingerprint、模型/tokenizer 信息、启动参数与
workload ID。单次服务的 `server.json` 记录实际执行命令、GPU/driver/CUDA/PyTorch、
Git 身份、状态；`pip-freeze.txt` 保存依赖。输入只保存摘要，不写完整 token 数组。

结果采用 UUID session，新运行不会覆盖旧 session。异常保留 `status=failed`、错误类型、
client 记录及日志；`status=collected` 只表示采集流程完成。机制是否观察到，还要检查
`observation.json` 的 `target_observed`。报告与云资源回收仍是独立完成标准。

## Trace 开关与 baseline

| 环境变量 | 用途 |
|---|---|
| `VLLM_KV_TRACE_DIR` | Week 9 的 block/request 事件 |
| `VLLM_KV_TRACE_LIMIT` | KV trace 上限，达到上限会写 `trace_truncated` |
| `VLLM_EXECUTION_CONFIG` | Week 10–12 的 execution/NVTX/profiler 配置 JSON |

服务启动时先清除继承的 study/profiler 开关，再仅启用本次需要的开关。Week 11/12
不会启用完整 Week 7/8/9 debug 日志。baseline 不启用 profiler、execution ranges 或
KV 日志；patched 函数保留快速关闭检查。保留这点细微 Python 包装开销，不宣称这是
与未打 patch 的发行版完全等价的性能基准。

Profiler 先让相同 workload 完成三轮模型预热，再创建 `armed` 文件。worker 看到该文件
才开始自己的 step schedule；因此不能把 API 端一次 HTTP 请求当作一个 profiler step。
一次请求可能跨很多 engine steps，一步也可以同时包含多条请求。

## 结果同步与停止 VM

使用已有结果同步/GCP 生命周期脚本，按实际 VM 参数执行，参考
[GCP 运行说明](gcp-spot-setup.md)。四周新增结果目录已加入 `.gitignore`。
保留原始 `.jsonl`、torch trace、`.nsys-rep`、SQLite 和命令后再做离线分析。
停止 server 不会停止 VM；报告要另外记录 VM 停止与残留资源检查证据。
