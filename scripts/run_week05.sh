#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec "${PYTHON:-python}" -m scripts.run_week05 --config "${CONFIG:-configs/week05.yaml}" "$@"
