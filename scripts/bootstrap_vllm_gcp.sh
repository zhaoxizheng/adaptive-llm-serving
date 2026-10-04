#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-.}"
VENV_NAME=".venv-vllm"
PINNED_VLLM_VERSION="0.10.2"

if [[ ! -d "${PROJECT_DIR}" ]]; then
  echo "Project directory does not exist: ${PROJECT_DIR}" >&2
  echo "Upload or clone the repository first." >&2
  exit 1
fi

cd "${PROJECT_DIR}"

if [[ -e "${VENV_NAME}" && ! -x "${VENV_NAME}/bin/python" ]]; then
  echo "${VENV_NAME} exists but is not a usable virtual environment." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y curl python3-venv

if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi >/dev/null 2>&1; then
  INSTALLER="${PWD}/cuda_installer.pyz"
  echo "Installing the NVIDIA LTS driver with Google's GPU installer..."
  curl -fL \
    https://storage.googleapis.com/compute-gpu-installation-us/installer/latest/cuda_installer.pyz \
    --output "${INSTALLER}"
  sudo python3 "${INSTALLER}" install_driver \
    --installation-mode=repo \
    --installation-branch=lts
  echo "Driver installation finished. Reboot if requested, then rerun this script."
  echo "GPU compatibility remains unverified until a generation smoke succeeds."
  exit 0
fi

if [[ ! -x "${VENV_NAME}/bin/python" ]]; then
  python3 -m venv "${VENV_NAME}"
fi

PYTHON_VERSION="$("${VENV_NAME}/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${PYTHON_VERSION}" != "3.12" ]]; then
  echo "Week 4 requires Python 3.12; ${VENV_NAME} uses ${PYTHON_VERSION}." >&2
  echo "Recreate ${VENV_NAME} with Ubuntu 24.04 python3." >&2
  exit 1
fi

RAW_DIR="results/week04/raw"
mkdir -p "${RAW_DIR}"
BOOTSTRAP_ENVIRONMENT="${RAW_DIR}/bootstrap-environment.txt"

{
  echo "captured_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "python=$("${VENV_NAME}/bin/python" --version 2>&1)"
  echo "pip=$("${VENV_NAME}/bin/python" -m pip --version 2>&1)"
  echo "gpu_compatibility=unverified until successful generation smoke"
  echo "nvidia_smi_begin"
  nvidia-smi
  echo "nvidia_smi_end"
} > "${BOOTSTRAP_ENVIRONMENT}"

"${VENV_NAME}/bin/python" -m pip install --upgrade pip

if [[ ! -f requirements-vllm.txt ]]; then
  echo "requirements-vllm.txt is required for the reproducible Week 4 environment." >&2
  exit 1
fi
if ! grep -Eq '^vllm==0\.10\.2([[:space:]]|$)' requirements-vllm.txt; then
  echo "requirements-vllm.txt must pin vllm==${PINNED_VLLM_VERSION}." >&2
  exit 1
fi
"${VENV_NAME}/bin/python" -m pip install -r requirements-vllm.txt

ACTUAL_VERSION="$("${VENV_NAME}/bin/python" -c 'from importlib.metadata import version; print(version("vllm"))')"
printf '%s\n' "${ACTUAL_VERSION}" > "${RAW_DIR}/vllm-version.txt"
if [[ "${ACTUAL_VERSION}" != "${PINNED_VLLM_VERSION}" ]]; then
  echo "Expected vllm==${PINNED_VLLM_VERSION}, got ${ACTUAL_VERSION}." >&2
  exit 1
fi

"${VENV_NAME}/bin/python" -m pip freeze --all > "${RAW_DIR}/pip-freeze.txt"
"${VENV_NAME}/bin/vllm" --help > "${RAW_DIR}/vllm-help.txt" 2>&1
"${VENV_NAME}/bin/vllm" serve --help > "${RAW_DIR}/vllm-serve-help.txt" 2>&1
"${VENV_NAME}/bin/vllm" bench serve --help > "${RAW_DIR}/bench-serve-help.txt" 2>&1

{
  echo "captured_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "python=$("${VENV_NAME}/bin/python" --version 2>&1)"
  echo "pip=$("${VENV_NAME}/bin/python" -m pip --version 2>&1)"
  echo "vllm=${ACTUAL_VERSION}"
  echo "gpu_compatibility=unverified until successful generation smoke"
  echo "nvidia_smi_begin"
  nvidia-smi
  echo "nvidia_smi_end"
} >> "${BOOTSTRAP_ENVIRONMENT}"

echo "Dedicated vLLM environment is ready: ${VENV_NAME} (vllm==${ACTUAL_VERSION})."
echo "GPU compatibility: unverified until a successful offline or HTTP generation smoke."
echo "Next: ${VENV_NAME}/bin/python scripts/start_vllm.py --config configs/week04.yaml plan"
