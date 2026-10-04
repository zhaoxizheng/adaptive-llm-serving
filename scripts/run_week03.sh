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
CALIBRATE="${CALIBRATE:-0}"
WEEK02_CONFIG="${WEEK02_CONFIG:-configs/week02.yaml}"

cd "${PROJECT_DIR}"
SOURCE_CONFIG="${CONFIG}"
prepare_args=(--config "${CONFIG}" --profile "${PROFILE}" --backend "${BACKEND}")
if [[ -n "${OUTPUT_ROOT}" ]]; then prepare_args+=(--output-root "${OUTPUT_ROOT}"); fi
if [[ -n "${RUN_LOG:-}" ]]; then prepare_args+=(--run-log "${RUN_LOG}"); fi
if [[ "${CALIBRATE}" == "1" ]]; then prepare_args+=(--calibrate); fi
CONFIG="$("${PYTHON}" -m scripts.prepare_week03_run "${prepare_args[@]}")"
RUN_ROOT="$("${PYTHON}" -m scripts.config_value --config "${CONFIG}" --key output.root)"
RUN_LOG="$("${PYTHON}" -m scripts.config_value --config "${CONFIG}" --key output.run_log)"
mkdir -p "$(dirname "${RUN_LOG}")"
{
  if [[ "${BACKEND}" == "hf" ]]; then
    if [[ "${CALIBRATE}" == "1" ]]; then
      "${PYTHON}" -m src.week03_calibration --week3-config "${CONFIG}" --week2-config "${WEEK02_CONFIG}"
    fi
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
    --output-root "${RUN_ROOT}"
  )
  if [[ -n "${MAX_CASES}" ]]; then
    arguments+=(--max-cases "${MAX_CASES}")
  fi

  echo "Week 3: profile=${PROFILE}, backend=${BACKEND}"
  "${PYTHON}" -m src.serve_week03 "${arguments[@]}"

  if [[ -z "${MAX_CASES}" ]]; then
    "${PYTHON}" -m src.analyze_week03 --config "${CONFIG}" --artifact-root "${RUN_ROOT}"
    if [[ "${BACKEND}" == "hf" && "${PROFILE}" == "primary" && -z "${OUTPUT_ROOT}" ]]; then
      echo "Official artifacts are ready. Complete reports/week03.md, then run:"
      echo "${PYTHON} -m scripts.verify_week03 --config ${SOURCE_CONFIG}"
    else
      echo "Simulation/smoke artifacts are ready at ${RUN_ROOT}; formal verification is intentionally disabled."
    fi
  else
    echo "Partial run requested; resume without MAX_CASES before analysis and verification."
  fi
} 2>&1 | tee -a "${RUN_LOG}"
