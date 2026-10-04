#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON="${PYTHON:-.venv/bin/python}"
CONFIG="${CONFIG:-configs/week03.yaml}"
PROFILE="${PROFILE:-primary}"
BACKEND="${BACKEND:-fake}"
MAX_CASES="${MAX_CASES:-}"
OUTPUT_ROOT="${OUTPUT_ROOT:-}"

cd "${PROJECT_DIR}"
RUN_LOG="${RUN_LOG:-$("${PYTHON}" -m scripts.config_value --config "${CONFIG}" --key output.run_log)}"
mkdir -p "$(dirname "${RUN_LOG}")"
exec > >(tee -a "${RUN_LOG}") 2>&1

if [[ "${BACKEND}" == "hf" ]]; then
  DEPENDENCY_FREEZE="$("${PYTHON}" -m scripts.config_value --config "${CONFIG}" --key output.dependency_freeze)"
  mkdir -p "$(dirname "${DEPENDENCY_FREEZE}")"
  "${PYTHON}" -m pip freeze --all > "${DEPENDENCY_FREEZE}"
  "${PYTHON}" -m scripts.prepare_week01_model --config "${CONFIG}"
  "${PYTHON}" -m scripts.check_env --config "${CONFIG}"
fi

arguments=(
  --config "${CONFIG}"
  --profile "${PROFILE}"
  --backend "${BACKEND}"
)
if [[ -n "${OUTPUT_ROOT}" ]]; then
  arguments+=(--output-root "${OUTPUT_ROOT}")
fi
if [[ -n "${MAX_CASES}" ]]; then
  arguments+=(--max-cases "${MAX_CASES}")
fi

echo "Week 3: profile=${PROFILE}, backend=${BACKEND}"
"${PYTHON}" -m src.serve_week03 "${arguments[@]}"

if [[ -z "${MAX_CASES}" ]]; then
  RUN_ROOT="${OUTPUT_ROOT}"
  if [[ -z "${RUN_ROOT}" ]]; then
    if [[ "${BACKEND}" == "fake" ]]; then
      RUN_ROOT="$("${PYTHON}" -m scripts.config_value --config "${CONFIG}" --key output.simulation_root)"
    elif [[ "${PROFILE}" != "primary" ]]; then
      RUN_ROOT="$("${PYTHON}" -m scripts.config_value --config "${CONFIG}" --key output.smoke_root)"
    fi
  fi
  if [[ -n "${RUN_ROOT}" ]]; then
    "${PYTHON}" -m src.analyze_week03 --config "${CONFIG}" --artifact-root "${RUN_ROOT}"
  else
    "${PYTHON}" -m src.analyze_week03 --config "${CONFIG}"
  fi
  if [[ "${BACKEND}" == "hf" && "${PROFILE}" == "primary" && -z "${OUTPUT_ROOT}" ]]; then
    echo "Official artifacts are ready. Complete reports/week03.md, then run:"
    echo "${PYTHON} -m scripts.verify_week03 --config ${CONFIG}"
  else
    echo "Simulation/smoke artifacts are ready at ${RUN_ROOT}; formal verification is intentionally disabled."
  fi
else
  echo "Partial run requested; resume without MAX_CASES before analysis and verification."
fi
