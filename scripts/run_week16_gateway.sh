#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec "${PYTHON:-python3.12}" -m scripts.run_cluster_study --config "${CONFIG:-configs/week16-l7-matrix.yaml}" "$@"
