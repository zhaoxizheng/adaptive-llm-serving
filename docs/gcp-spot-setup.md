# GCP L4 Spot Setup for Week 1

GCP is the primary GPU platform for this project. Week 1 uses one `g2-standard-4` Spot VM: 4 vCPUs, 16 GiB host memory, and one NVIDIA L4 with 24 GB VRAM. The default zone is `us-central1-a`; choose another zone in the same region if L4 Spot capacity is unavailable.

## Cost and Interruption Model

- Spot compute pricing can change and capacity is not guaranteed. Check the price displayed by GCP before creation.
- A Spot VM can be preempted at any time and has no availability SLA.
- This project sets the termination action to `STOP`, preserving the boot disk for restart.
- Stopped VM compute does not accrue compute charges, but the persistent disk continues to accrue storage charges.
- The benchmark persists each case atomically and resumes completed cases after restart.

Use a billing budget and alert as a guardrail, but do not treat an alert as an automatic hard spending cap.

## 1. Prerequisites

On the Mac, install and initialize the Google Cloud CLI, then select an existing project with Billing enabled:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable compute.googleapis.com
```

Check that the project has quota for G2 CPUs and preemptible/Spot NVIDIA L4 GPUs in the selected region. Quota availability is project-specific. If creation fails with a quota error, request the exact quota named in the error; do not switch to a larger G2 machine just to bypass the check.

Set the project and zone for all helper commands:

```bash
export GCP_PROJECT_ID=YOUR_PROJECT_ID
export GCP_ZONE=us-central1-a
```

Optional overrides are `GCP_VM_NAME`, `GCP_MACHINE_TYPE`, `GCP_IMAGE_FAMILY`, and `GCP_DISK_GB`. Keep the project defaults fixed for the Week 1 report.

## 2. Create the Spot VM

Review the command in `scripts/gcp_vm.sh`, then create the billable resource explicitly:

```bash
scripts/gcp_vm.sh create
scripts/gcp_vm.sh status
```

The default configuration is:

| Setting | Value |
|---|---|
| Machine type | `g2-standard-4` |
| GPU | 1 × NVIDIA L4 24 GB |
| Provisioning | Spot |
| Preemption action | Stop |
| Boot disk | 100 GB `pd-balanced` |
| Image family | `ubuntu-2404-lts-amd64` |
| Image project | `ubuntu-os-cloud` |

G2 does not support using Deep Learning VM images as its boot disk. The project therefore uses a supported Ubuntu image, Google's GPU driver installer, and a CUDA-enabled PyTorch wheel. To select a different supported public image, override both image variables explicitly:

```bash
export GCP_IMAGE_PROJECT=ubuntu-os-cloud
export GCP_IMAGE_FAMILY=ubuntu-2404-lts-amd64
```

## 3. Upload and Bootstrap

The project has no remote repository yet, so upload the working tree from the Mac:

```bash
scripts/upload_to_gcp.sh
scripts/gcp_vm.sh ssh
```

On the VM:

```bash
cd ~/adaptive-llm-serving
bash scripts/bootstrap_gcp.sh .
```

The first bootstrap run installs Google's recommended LTS NVIDIA driver and might reboot the VM. If the SSH connection closes or the script asks for a reboot, reconnect and rerun it:

```bash
cd ~/adaptive-llm-serving
bash scripts/bootstrap_gcp.sh .
nvidia-smi
make smoke PYTHON=.venv/bin/python
```

Once the driver is available, the bootstrap creates `.venv`, installs the CUDA 12.8 PyTorch wheel and the remaining dependency ranges, then writes `results/week01/environment.json`. Override `PYTORCH_INDEX_URL` only when deliberately testing another compatible PyTorch CUDA build. Use `.venv/bin/python` for subsequent Make commands.

## 4. Run and Resume the Benchmark

On the VM:

```bash
cd ~/adaptive-llm-serving
make benchmark PYTHON=.venv/bin/python
make report PYTHON=.venv/bin/python
```

If the VM is preempted, start it from the Mac and reconnect:

```bash
scripts/gcp_vm.sh start
scripts/gcp_vm.sh ssh
```

Then rerun `make benchmark PYTHON=.venv/bin/python`. Completed cases in `kv_cache.csv` are skipped. Do not upload a new working tree over the remote `results/` directory before resuming.

## 5. Download Results and Stop Billing

From the Mac:

```bash
scripts/sync_results_from_gcp.sh ./gcp-results
scripts/gcp_vm.sh stop
scripts/gcp_vm.sh status
```

Verify these files locally before deleting the VM:

- `gcp-results/results/week01/environment.json`
- `gcp-results/results/week01/raw/kv_cache.csv`
- `gcp-results/results/week01/raw/run_metadata.json`
- `gcp-results/results/week01/figures/generation-time.png`
- `gcp-results/results/week01/figures/output-throughput.png`

When the VM and its boot disk are no longer needed:

```bash
scripts/gcp_vm.sh delete
```

Deletion requires typing the VM name and removes the boot disk, so only run it after checking the downloaded results.

## Common Failures

### No Spot capacity

Try another G2-supported zone in `us-central1`, or wait and retry. Keep the GPU model fixed within one report.

### Quota exceeded

Open the GCP Quotas page for the project and region. Request only the quota identified by the create error. Approval is not guaranteed or immediate.

### CUDA is unavailable

Rerun `bash scripts/bootstrap_gcp.sh .`; Google's installer may need a reboot and a second run. Then verify with `nvidia-smi`. If it still fails, stop the VM before investigating so GPU compute is not billed while idle.

### VM was stopped during a benchmark

Start it, reconnect, and repeat the benchmark command. The last fully persisted case and every earlier case are retained; at most the case running at interruption must be repeated.

## Official References

- [Create a G2 or G4 instance](https://cloud.google.com/compute/docs/gpus/create-gpu-vm-g-series)
- [GPU machine types and G2 limitations](https://cloud.google.com/compute/docs/accelerator-optimized-machines)
- [Install NVIDIA GPU drivers](https://cloud.google.com/compute/docs/gpus/install-drivers-gpu)
- [Create and use Spot VMs](https://cloud.google.com/compute/docs/instances/create-use-spot)
- [GPU regions and zones](https://cloud.google.com/compute/docs/gpus/gpu-regions-zones)
- [Compute Engine pricing](https://cloud.google.com/compute/vm-instance-pricing)
