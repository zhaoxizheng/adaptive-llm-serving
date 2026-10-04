#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON="${PYTHON:-.venv/bin/python}"
CONFIG="${CONFIG:-configs/week02.yaml}"

cd "${PROJECT_DIR}"
RUN_LOG="${RUN_LOG:-$(
  "${PYTHON}" -m scripts.config_value --config "${CONFIG}" --key output.run_log
)}"
mkdir -p "$(dirname "${RUN_LOG}")"
exec > >(tee -a "${RUN_LOG}") 2>&1

on_exit() {
  local exit_code=$?
  if [[ ${exit_code} -ne 0 ]]; then
    "${PYTHON}" -m scripts.write_week01_status \
      --config "${CONFIG}" --status failed --exit-code "${exit_code}" || true
  fi
  exit "${exit_code}"
}
trap on_exit EXIT

"${PYTHON}" -m scripts.write_week01_status --config "${CONFIG}" --status running
DEPENDENCY_FREEZE="$(
  "${PYTHON}" -m scripts.config_value --config "${CONFIG}" --key output.dependency_freeze
)"
mkdir -p "$(dirname "${DEPENDENCY_FREEZE}")"
"${PYTHON}" -m pip freeze --all > "${DEPENDENCY_FREEZE}"
"${PYTHON}" -m scripts.prepare_week01_model --config "${CONFIG}"
"${PYTHON}" -m scripts.check_env --config "${CONFIG}"
"${PYTHON}" -m src.benchmark_batch --config "${CONFIG}"
"${PYTHON}" -m src.analyze_week02 --config "${CONFIG}"
"${PYTHON}" -m scripts.write_week01_status \
  --config "${CONFIG}" --status artifacts_ready --exit-code 0
echo "Week 2 artifacts are ready. Complete reports/week02.md, then run the verifier."

trap - EXIT
