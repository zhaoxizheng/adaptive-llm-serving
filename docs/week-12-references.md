# Week 12 Reference Reading

第十二周使用 Nsight Systems 回答 CPU 与 GPU 如何协作。重点是 timeline 语义、可控 capture、NVTX 标记和 CUDA Graph 对比；单 kernel counters 留到 Week 13。

PyTorch Profiler、CUDA timing 和 vLLM CUDA Graph 设计分别复用 Week 11 Reference #1–4、Week 1 Reference #4–6 和 Week 10 Reference #3–4，不重复列出 URL。

## 必读：Nsight Systems

1. [NVIDIA Nsight Systems User Guide](https://docs.nvidia.com/nsight-systems/UserGuide/index.html)
   - 阅读 CLI profiling、delay/duration、trace domains、report 和 export。
   - 只启用回答当前问题所需的数据源，控制开销和文件大小。

2. [NVIDIA Nsight Systems Analysis Guide](https://docs.nvidia.com/nsight-systems/AnalysisGuide/index.html)
   - 学习 CUDA API、GPU kernel、memory operation、OS runtime 与统计表的解释。
   - 使用证据给 idle gap 分类，不凭 timeline 外观下结论。

## 必读：NVTX 与 CUDA Graph

3. [NVIDIA Tools Extension SDK](https://nvidia.github.io/NVTX/)
   - 理解 range、mark、domain、thread/process 边界和命名规则。
   - 用稳定短名称标注 prepare、execute、sample 和 scheduler step。

4. [NVIDIA: Getting Started with CUDA Graphs](https://developer.nvidia.com/blog/cuda-graphs/)
   - 理解传统 launch sequence 与 graph launch 的差异。
   - 把概念与 Nsight 中实际捕获的 API/kernel pattern 对照。

## 需要复用的前置资料

- vLLM profiling 启动与范围控制：复用 Week 11 Reference #1。
- PyTorch 自定义 profiler ranges：复用 Week 11 Reference #4。
- vLLM/PyTorch CUDA Graph 机制：复用 Week 10 Reference #3–4。
- Scheduler 与 chunked prefill：复用 Week 8 Reference #1、#3。

## 阅读顺序

| 日期 | 阅读 | 对应任务 |
|---|---|---|
| Day 1 | 1 | 最小 capture 与 CLI contract |
| Day 2 | 3，复用 Week 11 #4 | NVTX 与跨层对齐 |
| Day 3–4 | 2 | prefill/decode timeline 与 gap 分类 |
| Day 5 | 4，复用 Week 10 #3–4 | CUDA Graph A/B |
| Day 6–7 | 回看 1–4 | mixed workload、RCA 与报告 |

## 阅读后的自测问题

1. CUDA API duration 与对应 GPU kernel duration 为什么不是同一件事？
2. Nsight capture 的 delay、duration、capture-range 和 trace domain 如何控制开销？
3. NVTX range 位于 CPU timeline 时，如何与异步 GPU work 建立联系？
4. 哪些证据可以区分 host launch gap、synchronization 和 workload bubble？
5. CUDA Graph replay 在 API row 和 GPU row 上分别有什么可观察变化？
6. 为什么 eager/graph A/B 必须固定 batch shape 与 scheduler composition？
7. 选择 Week 13 kernel target 时，除了总 duration 还要考虑哪些因素？
