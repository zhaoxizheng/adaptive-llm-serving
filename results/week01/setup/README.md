# Week 1 GPU setup

Environment setup and the 32-token generation smoke passed on 2026-10-09.
The full KV Cache benchmark has not been run.
After evidence was synced and validated locally, the VM was stopped and its
temporary bootstrap SSH key was removed. The boot disk retains the environment,
model cache, and source tree for the next session.

- Project: `adaptive-llm-serving`
- VM: `adaptive-llm-week01`, `us-central1-a`
- Hardware: `g2-standard-4`, one NVIDIA L4, 100 GB balanced persistent disk
- Image: `ubuntu-2404-noble-amd64-v20260918`
- Working directory: `/home/llmlearner/adaptive-llm-serving`
- Source commit: `2aedc4730a66d62d37cf03e21a8b113876a894dd`
- Kernel: `7.0.0-1014-gcp`
- NVIDIA driver: `580.178.04`
- Python: `3.12.3`
- PyTorch: `2.8.0+cu128`; CUDA runtime: `12.8`
- Transformers: `4.46.3`
- Model: `Qwen/Qwen2.5-0.5B-Instruct`, revision `7ae557604adf67be50417f59c2c2f167def9a775`
- Dtype: `bfloat16`

## Driver installer compatibility

The Google installer rejected the repository bootstrap's combination of
`--installation-mode=repo --installation-branch=lts`. Its error required binary
installation mode for the LTS branch. Driver installation therefore used:

```bash
sudo python3 cuda_installer.pyz install_driver \
  --installation-mode=binary --installation-branch=lts
```

The installer upgraded the GCP kernel and rebooted the VM. The same command was
run again after reboot to finish driver installation. The original
`bash scripts/bootstrap_gcp.sh .` then completed successfully. No experiment
source files were modified on the VM.

The installer was downloaded from
`https://storage.googleapis.com/compute-gpu-installation-us/installer/latest/cuda_installer.pyz`.
Its SHA-256 is recorded in `cuda-installer.sha256`; the installation logs are
preserved in this directory, including the initial parameter error.

## Validation

- `nvidia-smi` reported NVIDIA L4, driver `580.178.04`, and 23034 MiB VRAM.
- `scripts.check_env` returned `valid`, with CUDA available and no validation errors.
- `pip check` reported no broken requirements.
- `src.generate` completed 32 output tokens on the GPU using BF16 and KV Cache.
- Local validation checked the source commit, pinned model revision, output token
  count, and dependency-freeze SHA-256 against the synced smoke evidence.

The smoke was a single functional check without the benchmark's warmups and
repeats. Its latency measurements should not be treated as benchmark results.

## SSH account

Use `llmlearner` explicitly with `gcloud`. Omitting the remote username makes
`gcloud` derive `admin` from the local macOS account. Ubuntu already has an
`admin` group, so the guest agent cannot create an `admin` user and SSH fails
with `Permission denied (publickey)`.

```bash
gcloud compute ssh llmlearner@adaptive-llm-week01 \
  --project=adaptive-llm-serving --zone=us-central1-a
```

After starting the VM and connecting as `llmlearner`, the next experiment command is:

```bash
cd /home/llmlearner/adaptive-llm-serving
make run-week01 PYTHON=.venv/bin/python
```
