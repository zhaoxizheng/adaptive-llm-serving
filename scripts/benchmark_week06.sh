#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec "${PYTHON:-python}" -m scripts.benchmark_week06 --config "${CONFIG:-configs/week06.yaml}" "$@"
