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

Optional overrides are `GCP_VM_NAME`, `GCP_MACHINE_TYPE`, `GCP_IMAGE_FAMILY`,
`GCP_IMAGE_NAME`, `GCP_DISK_GB`, and `GCP_MAX_RUN_DURATION`. Creation resolves the
image family once and then passes the concrete image name to GCE. The default
maximum run duration is six hours, after which GCE stops the VM. Keep the resolved
image and project defaults fixed in the Week 1 report.

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

Upload the clean, committed working tree from the Mac. The upload command refuses a
dirty source and creates `.experiment-source.json`, so the non-Git VM records the
real local commit rather than `uncommitted`:

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
make run-week01 PYTHON=.venv/bin/python
```

Once the driver is available, the bootstrap creates a Python 3.12 `.venv`, installs
PyTorch 2.8.0 from the CUDA 12.8 index and the exact application dependencies,
saves `dependency-freeze.txt`, validates the CUDA environment, and downloads the
pinned model commit with a file-level SHA-256 inventory. Override versions only for
a separate experiment; an existing Week 1 run will refuse to resume after runtime
identity changes.

## 4. Run and Resume the Benchmark

On the VM:

```bash
cd ~/adaptive-llm-serving
make run-week01 PYTHON=.venv/bin/python
```

The runner captures a structured smoke result, a combined stdout/stderr log, the
full resumable benchmark, both figures, and `run-status.json`. Do not write the
report while the GPU VM is billing.

If the VM is preempted, start it from the Mac and reconnect:

```bash
scripts/gcp_vm.sh start
scripts/gcp_vm.sh ssh
```

Then rerun `make run-week01 PYTHON=.venv/bin/python`. Its benchmark stage validates
the existing evidence and skips completed cases, while the remaining stages rebuild
figures and restore `run-status.json` to `artifacts_ready`. Do not upload a new
working tree over the remote `results/` directory before resuming.

## 5. Download Results and Stop Billing

From the Mac:

```bash
scripts/sync_results_from_gcp.sh .
scripts/gcp_vm.sh stop
scripts/gcp_vm.sh status
```

Now work locally: complete `reports/week01.md` using the synced raw data and figures,
then create a small local Python 3.12 environment containing PyYAML and run
`make verify PYTHON=<that-python>`. Verification does not use
CUDA; on success it writes `results/week01/verification-receipt.json` and marks the
durable run status `completed`. It rejects changes to experiment code but permits
the expected local report edit. Commit the report and selected evidence only after
reviewing their size and contents.

Verify these files locally before deleting the VM:

- `results/week01/environment.json`
- `results/week01/dependency-freeze.txt`
- `results/week01/model-snapshot.json`
- `results/week01/smoke.json`
- `results/week01/logs/week01.log`
- `results/week01/raw/kv_cache.csv`
- `results/week01/raw/run_metadata.json`
- `results/week01/figures/generation-time.png`
- `results/week01/figures/output-throughput.png`
- `results/week01/verification-receipt.json` after local report verification

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
