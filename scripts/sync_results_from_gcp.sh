#!/usr/bin/env bash
set -euo pipefail

GCP_PROJECT_ID="${GCP_PROJECT_ID:-}"
GCP_ZONE="${GCP_ZONE:-us-central1-a}"
GCP_VM_NAME="${GCP_VM_NAME:-adaptive-llm-week01}"
REMOTE_DIR="${REMOTE_DIR:-adaptive-llm-serving}"
LOCAL_OUTPUT="${1:-./gcp-results}"

if [[ -z "${GCP_PROJECT_ID}" ]]; then
  echo "Set GCP_PROJECT_ID before running this script." >&2
  exit 2
fi

mkdir -p "${LOCAL_OUTPUT}"
gcloud compute scp --recurse \
  "${GCP_VM_NAME}:~/${REMOTE_DIR}/results" \
  "${LOCAL_OUTPUT}/" \
  --project="${GCP_PROJECT_ID}" --zone="${GCP_ZONE}"

echo "Downloaded results to ${LOCAL_OUTPUT}"
