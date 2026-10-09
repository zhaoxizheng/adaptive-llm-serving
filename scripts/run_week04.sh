#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${CONFIG:-configs/week04.yaml}"
# 默认采集 GPU 证据；计划、离线分析、审计模板和最终验收使用独立模式。
MODE="gpu"

usage() {
  cat <<'EOF'
Usage: scripts/run_week04.sh [--plan|--gpu|--analyze|--audit-template|--verify]

  --plan            Validate and print the CPU-only execution plan.
  --gpu             Collect one identity-bound GPU run (default).
  --analyze         Re-normalize raw evidence and create offline analysis artifacts.
  --audit-template  Create an unconfirmed resource-audit template after result sync.
  --verify          Perform final offline verification after report and audit completion.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan|--dry-run) MODE="plan"; shift ;;
    --gpu) MODE="gpu"; shift ;;
    --analyze|--offline-analysis) MODE="analyze"; shift ;;
    --audit-template) MODE="audit-template"; shift ;;
    --verify|--final-verify) MODE="verify"; shift ;;
    --config)
      [[ $# -ge 2 ]] || { echo "--config requires a path" >&2; exit 2; }
      CONFIG="$2"
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

cd "${PROJECT_DIR}"
# vLLM 使用独立环境，避免其依赖与 HF/离线分析环境相互覆盖。
CONTRACT_PYTHON="${CONTRACT_PYTHON:-python}"
GPU_PYTHON="${VLLM_PYTHON:-${PYTHON:-.venv-vllm/bin/python}}"
ANALYSIS_PYTHON="${ANALYSIS_PYTHON:-${PYTHON:-.venv/bin/python}}"

# 只展开配置与命令计划，不启动 GPU 服务，也不产生正式运行证据。
if [[ "${MODE}" == "plan" ]]; then
  "${CONTRACT_PYTHON}" -m src.vllm_contract validate --config "${CONFIG}"
  "${CONTRACT_PYTHON}" -m src.vllm_contract server-plan --config "${CONFIG}"
  "${CONTRACT_PYTHON}" -m src.vllm_contract cases --config "${CONFIG}"
  "${CONTRACT_PYTHON}" -m src.vllm_offline_smoke --config "${CONFIG}" --plan
  "${CONTRACT_PYTHON}" -m src.openai_smoke --config "${CONFIG}" --plan
  echo "Plan only: no environment, run ID, GPU compatibility, or benchmark evidence was created."
  exit 0
fi

# 离线分析从 raw 重建统一 schema，并要求 Week 3 对比证据存在。
# 图表准备完成仍不等于验收完成：报告和资源审计还需补齐。
if [[ "${MODE}" == "analyze" ]]; then
  if [[ ! -x "${ANALYSIS_PYTHON}" ]]; then
    echo "Analysis environment missing: ${ANALYSIS_PYTHON}" >&2
    exit 1
  fi
  RAW_DIR="$("${ANALYSIS_PYTHON}" -m scripts.config_value --config "${CONFIG}" --key output.raw_dir)"
  NORMALIZED="$("${ANALYSIS_PYTHON}" -m scripts.config_value --config "${CONFIG}" --key output.normalized_jsonl)"
  WEEK03_RESULTS="$("${ANALYSIS_PYTHON}" -m scripts.config_value --config "${CONFIG}" --key benchmark.comparison.week03_results)"
  CASE_IDS=()
  while IFS= read -r case_id; do
    CASE_IDS+=("${case_id}")
  done < <(
    "${ANALYSIS_PYTHON}" -m src.vllm_contract cases --config "${CONFIG}" |
      "${ANALYSIS_PYTHON}" -c 'import json,sys; [print(row["case_id"]) for row in json.load(sys.stdin)]'
  )
  NORMALIZE_ARGV=("${ANALYSIS_PYTHON}" -m src.vllm_result_adapter --output "${NORMALIZED}")
  for case_id in "${CASE_IDS[@]}"; do
    raw_path="${RAW_DIR}/benchmark/${case_id}.json"
    if [[ ! -s "${raw_path}" ]]; then
      echo "Missing raw benchmark case for offline analysis: ${raw_path}" >&2
      exit 1
    fi
    NORMALIZE_ARGV+=(--input "${raw_path}")
  done
  "${NORMALIZE_ARGV[@]}"
  if [[ ! -s "${WEEK03_RESULTS}" ]]; then
    echo "Week 3 comparison evidence is missing: ${WEEK03_RESULTS}" >&2
    echo "Raw Week 4 evidence remains intact; analysis was not claimed complete." >&2
    exit 1
  fi
  "${ANALYSIS_PYTHON}" -m src.analyze_week04 \
    --config "${CONFIG}" --week03-input "${WEEK03_RESULTS}"
  "${ANALYSIS_PYTHON}" -m scripts.week04_lifecycle --config "${CONFIG}" \
    status --status artifacts_ready --exit-code 0 --phase offline_analysis_complete
  echo "Offline analysis is ready. Complete the report and resource audit before --verify."
  exit 0
fi

if [[ "${MODE}" == "audit-template" ]]; then
  if [[ ! -x "${ANALYSIS_PYTHON}" ]]; then
    echo "Analysis environment missing: ${ANALYSIS_PYTHON}" >&2
    exit 1
  fi
  "${ANALYSIS_PYTHON}" -m scripts.week04_lifecycle --config "${CONFIG}" audit-template
  echo "The template is intentionally unconfirmed. Fill it only after the VM is stopped and resources are audited."
  exit 0
fi

if [[ "${MODE}" == "verify" ]]; then
  if [[ ! -x "${ANALYSIS_PYTHON}" ]]; then
    echo "Analysis environment missing: ${ANALYSIS_PYTHON}" >&2
    exit 1
  fi
  "${ANALYSIS_PYTHON}" scripts/verify_week04.py --config "${CONFIG}"
  exit 0
fi

if [[ ! -x "${GPU_PYTHON}" ]]; then
  echo "Dedicated vLLM runtime missing: ${GPU_PYTHON}; run scripts/bootstrap_vllm_gcp.sh first." >&2
  exit 1
fi

SERVER_STARTED=false
RUN_INITIALIZED=false
CURRENT_PHASE="preflight"
# 异常退出时停止本次启动的服务，确认清理结果，并记录失败发生的阶段。
cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM HUP
  local cleanup_failed=false
  if ${SERVER_STARTED}; then
    if ! "${GPU_PYTHON}" scripts/start_vllm.py --config "${CONFIG}" stop; then
      cleanup_failed=true
      exit_code=1
    else
      SERVER_STARTED=false
    fi
  fi
  if ${RUN_INITIALIZED} && [[ ${exit_code} -ne 0 ]]; then
    local reason="Week 4 GPU phase failed during ${CURRENT_PHASE} (exit ${exit_code})"
    if ${cleanup_failed}; then
      reason="${reason}; owned server cleanup or port-close confirmation failed"
    fi
    "${GPU_PYTHON}" -m scripts.week04_lifecycle --config "${CONFIG}" \
      status --status failed --exit-code "${exit_code}" \
      --phase "${CURRENT_PHASE}" --reason "${reason}" || true
  fi
  exit "${exit_code}"
}
trap cleanup EXIT INT TERM HUP

CURRENT_PHASE="initialize"
"${GPU_PYTHON}" -m src.vllm_contract validate --config "${CONFIG}"
"${GPU_PYTHON}" -m scripts.week04_lifecycle --config "${CONFIG}" initialize >/dev/null
RUN_INITIALIZED=true
"${GPU_PYTHON}" -m scripts.week04_lifecycle --config "${CONFIG}" manifest >/dev/null

RAW_DIR="$("${GPU_PYTHON}" -m scripts.config_value --config "${CONFIG}" --key output.raw_dir)"
BENCHMARK_TIMEOUT="$("${GPU_PYTHON}" -m scripts.config_value --config "${CONFIG}" --key benchmark.timeout_seconds)"
SHUTDOWN_GRACE="$("${GPU_PYTHON}" -m scripts.config_value --config "${CONFIG}" --key server.shutdown_timeout_seconds)"
mkdir -p "${RAW_DIR}/watchdogs"

# 为子进程设置总超时、终止宽限期和独立日志，避免 GPU 阶段无限等待。
run_bounded_phase() {
  local phase="$1"
  shift
  CURRENT_PHASE="${phase}"
  "${GPU_PYTHON}" -m scripts.week04_lifecycle --config "${CONFIG}" \
    status --status running --phase "${phase}"
  "${GPU_PYTHON}" -m src.process_watchdog \
    --timeout-seconds "${BENCHMARK_TIMEOUT}" \
    --term-grace-seconds "${SHUTDOWN_GRACE}" \
    --stdout "${RAW_DIR}/watchdogs/${phase}.stdout.txt" \
    --result "${RAW_DIR}/watchdogs/${phase}.json" -- "$@"
}

# 先验证直接模型推理，再启动 HTTP 服务并验证普通响应和 SSE 流式响应。
run_bounded_phase offline-smoke \
  "${GPU_PYTHON}" -m src.vllm_offline_smoke --config "${CONFIG}"

CURRENT_PHASE="server-start"
"${GPU_PYTHON}" -m scripts.week04_lifecycle --config "${CONFIG}" \
  status --status running --phase server-start
"${GPU_PYTHON}" scripts/start_vllm.py --config "${CONFIG}" start
SERVER_STARTED=true

run_bounded_phase openai-smoke \
  "${GPU_PYTHON}" -m src.openai_smoke --config "${CONFIG}" --mode both

CASE_IDS=()
while IFS= read -r case_id; do
  CASE_IDS+=("${case_id}")
done < <(
  "${GPU_PYTHON}" -m src.vllm_contract cases --config "${CONFIG}" |
    "${GPU_PYTHON}" -c 'import json,sys; [print(row["case_id"]) for row in json.load(sys.stdin)]'
)
if [[ ${#CASE_IDS[@]} -eq 0 ]]; then
  echo "Week 4 contract expanded to zero cases." >&2
  exit 1
fi

# 首个用例先跑压测 smoke；随后遍历全部正式 case，smoke 不替代正式测量。
CURRENT_PHASE="benchmark-smoke"
"${GPU_PYTHON}" -m scripts.week04_lifecycle --config "${CONFIG}" \
  status --status running --phase benchmark-smoke
VLLM_PYTHON="${GPU_PYTHON}" bash scripts/benchmark_vllm.sh \
  --config "${CONFIG}" --case-id "${CASE_IDS[0]}" --smoke

for case_id in "${CASE_IDS[@]}"; do
  CURRENT_PHASE="benchmark:${case_id}"
  "${GPU_PYTHON}" -m scripts.week04_lifecycle --config "${CONFIG}" \
    status --status running --phase "${CURRENT_PHASE}"
  VLLM_PYTHON="${GPU_PYTHON}" bash scripts/benchmark_vllm.sh \
    --config "${CONFIG}" --case-id "${case_id}"
done

# 原样重放 Week 3 已保存的到达轨迹，作为独立的开环对比证据。
# 客户端根据这些 trace 创建并验证对比 manifest，不与官方 CLI 压测混为一组。
CURRENT_PHASE="trace-replay"
"${GPU_PYTHON}" -m scripts.week04_lifecycle --config "${CONFIG}" \
  status --status running --phase trace-replay
"${GPU_PYTHON}" -m src.openai_trace_client --config "${CONFIG}" --all-configured

CURRENT_PHASE="server-stop"
"${GPU_PYTHON}" scripts/start_vllm.py --config "${CONFIG}" stop
SERVER_STARTED=false

# 停止服务不等于停止云 VM；GPU 证据就绪后还需同步、停机、分析、审计和最终验收。
CURRENT_PHASE="gpu-artifacts-ready"
"${GPU_PYTHON}" -m scripts.week04_lifecycle --config "${CONFIG}" \
  status --status running --exit-code 0 --phase gpu_artifacts_ready
echo "GPU evidence is ready, but the run is not verified."
echo "Sync results, stop the VM, run --analyze, complete the report/audit, then run --verify."
trap - EXIT INT TERM HUP
