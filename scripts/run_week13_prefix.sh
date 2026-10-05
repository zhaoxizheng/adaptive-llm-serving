#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec "${PYTHON:-python3.12}" -m scripts.run_serving_experiment --config "${CONFIG:-configs/week13-prefix.yaml}" "$@"
