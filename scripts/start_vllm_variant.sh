#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec "${PYTHON:-python}" -m scripts.start_vllm_variant --config "${CONFIG:-configs/week06.yaml}" "$@"
