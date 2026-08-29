#!/usr/bin/env bash
set -euo pipefail

GCP_PROJECT_ID="${GCP_PROJECT_ID:-}"
GCP_ZONE="${GCP_ZONE:-us-central1-a}"
GCP_VM_NAME="${GCP_VM_NAME:-adaptive-llm-week01}"
REMOTE_DIR="${REMOTE_DIR:-adaptive-llm-serving}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -z "${GCP_PROJECT_ID}" ]]; then
  echo "Set GCP_PROJECT_ID before running this script." >&2
  exit 2
fi

gcloud compute ssh "${GCP_VM_NAME}" \
  --project="${GCP_PROJECT_ID}" --zone="${GCP_ZONE}" \
  --command="mkdir -p ${REMOTE_DIR}"

gcloud compute scp --recurse \
  "${PROJECT_DIR}/.gitignore" \
  "${PROJECT_DIR}/Makefile" \
  "${PROJECT_DIR}/README.md" \
  "${PROJECT_DIR}/pyproject.toml" \
  "${PROJECT_DIR}/requirements.txt" \
  "${PROJECT_DIR}/requirements-dev.txt" \
  "${PROJECT_DIR}/configs" \
  "${PROJECT_DIR}/docs" \
  "${PROJECT_DIR}/reports" \
  "${PROJECT_DIR}/scripts" \
  "${PROJECT_DIR}/src" \
  "${PROJECT_DIR}/tests" \
  "${GCP_VM_NAME}:~/${REMOTE_DIR}/" \
  --project="${GCP_PROJECT_ID}" --zone="${GCP_ZONE}"

echo "Uploaded project to ${GCP_VM_NAME}:~/${REMOTE_DIR}"
