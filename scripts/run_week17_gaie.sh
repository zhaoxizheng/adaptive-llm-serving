#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec "${PYTHON:-python3.12}" -m scripts.run_platform_study --config "${CONFIG:-configs/week17-inferencepool.yaml}" "$@"
