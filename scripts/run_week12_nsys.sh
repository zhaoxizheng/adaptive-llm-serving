#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec "${PYTHON:-python}" -m scripts.run_deep_study --config "${CONFIG:-configs/week12-nsys.yaml}" "$@"
