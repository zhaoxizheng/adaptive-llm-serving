#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-.}"
CONFIG="${CONFIG:-configs/week01.yaml}"
PYTORCH_INDEX_URL="${PYTORCH_INDEX_URL:-https://download.pytorch.org/whl/cu128}"
PYTORCH_VERSION="${PYTORCH_VERSION:-2.8.0+cu128}"

if [[ ! -d "${PROJECT_DIR}" ]]; then
  echo "Project directory does not exist: ${PROJECT_DIR}" >&2
  echo "Upload or clone the repository first." >&2
  exit 1
fi

cd "${PROJECT_DIR}"
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
  exit 0
fi

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi

if [[ "$(.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" != "3.12" ]]; then
  echo "Week 1 requires Python 3.12; recreate .venv with Ubuntu 24.04 python3." >&2
  exit 1
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install "torch==${PYTORCH_VERSION}" --index-url "${PYTORCH_INDEX_URL}"
.venv/bin/python -m pip install -r requirements.txt
DEPENDENCY_FREEZE="$(.venv/bin/python -m scripts.config_value \
  --config "${CONFIG}" --key output.dependency_freeze)"
mkdir -p "$(dirname "${DEPENDENCY_FREEZE}")"
.venv/bin/python -m pip freeze --all > "${DEPENDENCY_FREEZE}"
.venv/bin/python -m scripts.prepare_week01_model --config "${CONFIG}"
.venv/bin/python -m scripts.check_env --config "${CONFIG}"

echo "GCP environment is ready. Next command: make run-week01 PYTHON=.venv/bin/python"
