#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-}"
GCP_PROJECT_ID="${GCP_PROJECT_ID:-}"
GCP_ZONE="${GCP_ZONE:-us-central1-a}"
GCP_VM_NAME="${GCP_VM_NAME:-adaptive-llm-week01}"
GCP_MACHINE_TYPE="${GCP_MACHINE_TYPE:-g2-standard-4}"
GCP_IMAGE_FAMILY="${GCP_IMAGE_FAMILY:-ubuntu-2404-lts-amd64}"
GCP_IMAGE_PROJECT="${GCP_IMAGE_PROJECT:-ubuntu-os-cloud}"
GCP_DISK_GB="${GCP_DISK_GB:-100}"

usage() {
  echo "Usage: GCP_PROJECT_ID=<project-id> $0 {create|start|stop|status|ssh|delete}" >&2
}

if [[ -z "${GCP_PROJECT_ID}" || -z "${ACTION}" ]]; then
  usage
  exit 2
fi

case "${ACTION}" in
  create)
    gcloud compute instances create "${GCP_VM_NAME}" \
      --project="${GCP_PROJECT_ID}" \
      --zone="${GCP_ZONE}" \
      --machine-type="${GCP_MACHINE_TYPE}" \
      --provisioning-model=SPOT \
      --instance-termination-action=STOP \
      --maintenance-policy=TERMINATE \
      --boot-disk-size="${GCP_DISK_GB}GB" \
      --boot-disk-type=pd-balanced \
      --image-family="${GCP_IMAGE_FAMILY}" \
      --image-project="${GCP_IMAGE_PROJECT}" \
      --labels=project=adaptive-llm-serving,purpose=learning
    ;;
  start)
    gcloud compute instances start "${GCP_VM_NAME}" \
      --project="${GCP_PROJECT_ID}" --zone="${GCP_ZONE}"
    ;;
  stop)
    gcloud compute instances stop "${GCP_VM_NAME}" \
      --project="${GCP_PROJECT_ID}" --zone="${GCP_ZONE}"
    ;;
  status)
    gcloud compute instances describe "${GCP_VM_NAME}" \
      --project="${GCP_PROJECT_ID}" --zone="${GCP_ZONE}" \
      --format='table(name,status,machineType.basename(),scheduling.provisioningModel,disks[0].diskSizeGb)'
    ;;
  ssh)
    gcloud compute ssh "${GCP_VM_NAME}" \
      --project="${GCP_PROJECT_ID}" --zone="${GCP_ZONE}"
    ;;
  delete)
    read -r -p "Delete ${GCP_VM_NAME} and its boot disk? Type the VM name to confirm: " CONFIRMATION
    if [[ "${CONFIRMATION}" != "${GCP_VM_NAME}" ]]; then
      echo "Deletion cancelled."
      exit 1
    fi
    gcloud compute instances delete "${GCP_VM_NAME}" \
      --project="${GCP_PROJECT_ID}" --zone="${GCP_ZONE}" --quiet
    ;;
  *)
    usage
    exit 2
    ;;
esac
