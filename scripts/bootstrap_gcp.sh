#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-.}"
PYTORCH_INDEX_URL="${PYTORCH_INDEX_URL:-https://download.pytorch.org/whl/cu128}"

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

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install torch --index-url "${PYTORCH_INDEX_URL}"
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m scripts.check_env --output results/week01/environment.json

echo "GCP environment is ready. Next command: make smoke PYTHON=.venv/bin/python"
