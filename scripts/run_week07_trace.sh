#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec "${PYTHON:-python}" -m scripts.run_source_study --config "${CONFIG:-configs/week07.yaml}" "$@"
