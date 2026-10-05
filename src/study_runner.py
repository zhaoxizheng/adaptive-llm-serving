"""Small, bounded single-instance experiment runner; no GPU imports at module load."""

from __future__ import annotations

import concurrent.futures
import contextlib
import csv
import importlib.metadata
import json
import os
import random
import signal
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.capture_run_metrics import capture
from src.common import source_identity, utc_now, write_json, write_text
from src.openai_stream import OpenAIHTTPError, http_json, iter_openai_events, stream_sse
from src.study_contract import VLLM_VERSION, fingerprint
from src.vllm_contract import is_loopback_host, validate_help_support


def timestamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def fetch_text(url):
    import urllib.request

    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read(16 * 1024 * 1024).decode("utf-8")


def runtime_identity(*, source_study=False):
    installed_version = importlib.metadata.version("vllm")
    if installed_version != VLLM_VERSION and not source_study:
        raise RuntimeError(f"these experiments require vllm=={VLLM_VERSION}")
    gpu = subprocess.check_output(
        [
            "nvidia-smi",
            "--query-gpu=name,uuid,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ],
        text=True,
        timeout=10,
    ).strip()
    if len(gpu.splitlines()) != 1 or "L4" not in gpu:
        raise RuntimeError("Week 5-8 baseline requires exactly one NVIDIA L4")
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable")
    freeze = subprocess.check_output(
        [os.sys.executable, "-m", "pip", "freeze", "--all"], text=True, timeout=30
    )
    return {
        "vllm_version": installed_version,
        "gpu": gpu,
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "dependency_freeze_sha256": fingerprint(freeze),
        "pip_freeze": freeze,
    }


@contextlib.contextmanager
def managed_server(base, argv, root, *, env=None, source_study=False, launch_prefix=None,
                   shutdown_signal=signal.SIGTERM, runtime_probe=None):
    """Own a new process group; never attach to or kill an existing server."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    host, port = base["server"]["host"], base["server"]["port"]
    if not is_loopback_host(host):
        raise ValueError("study server must bind loopback")
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as probe:
        if probe.connect_ex((host, port)) == 0:
            raise RuntimeError(f"port {port} already has a listener")
    url = f"http://{'[' + host + ']' if ':' in host else host}:{port}"
    # Source-study callers verified the exact commit, patch bytes and import path.
    # Editable builds can carry a local/dev suffix despite matching that source.
    runtime = (runtime_probe() if runtime_probe else runtime_identity(
        source_study=source_study or bool(env and env.get("VLLM_STUDY_TRACE_DIR"))))
    binary = Path(os.sys.executable).parent / "vllm"
    if not binary.is_file():
        raise RuntimeError("vllm CLI is missing from the selected Python environment")
    launch_argv = [*(launch_prefix or []), str(binary), *argv[1:]]
    help_text = subprocess.check_output([str(binary), "serve", "--help=all"], text=True, timeout=60)
    validate_help_support(argv, help_text)
    write_text(root / "serve-help.txt", help_text)
    write_text(root / "pip-freeze.txt", runtime.pop("pip_freeze"))
    metadata = {
        "server_id": str(uuid.uuid4()),
        "argv": argv,
        "executed_argv": launch_argv,
        "runtime": runtime,
        "source": source_identity(),
        "started_at": utc_now(),
        "status": "starting",
    }
    write_json(root / "server.json", metadata)
    environment = {**os.environ, "VLLM_USE_V1": "1"}
    environment.pop("VLLM_STUDY_TRACE_DIR", None)
    environment.pop("VLLM_KV_TRACE_DIR", None)
    environment.pop("VLLM_EXECUTION_CONFIG", None)
    environment.pop("VLLM_TORCH_PROFILER_DIR", None)
    environment.update(env or {})
    process = None
    with (root / "server.log").open("w", encoding="utf-8") as log:
        try:
            started = time.monotonic()
            process = subprocess.Popen(
                launch_argv,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=environment,
                start_new_session=True,
            )
            deadline = started + base["server"]["startup_timeout_seconds"]
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("vLLM exited during startup; inspect server.log")
                try:
                    models = http_json(url + "/v1/models", timeout=2)
                    if any(m["id"] == base["model"]["served_model_name"] for m in models["data"]):
                        break
                except (OpenAIHTTPError, KeyError, TypeError):
                    pass
                time.sleep(base["server"].get("readiness_poll_seconds", 1))
            else:
                raise TimeoutError("vLLM readiness deadline exceeded")
            metadata.update(
                status="ready",
                startup_seconds=time.monotonic() - started,
                pid=process.pid,
                models=models,
            )
            write_json(root / "server.json", metadata)
            yield url, metadata
        except BaseException as error:
            metadata.update(status="failed", error_type=type(error).__name__)
            raise
        finally:
            if process is not None:
                # The freshly created session belongs to this context, even if its leader exits.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, shutdown_signal)
                deadline = time.monotonic() + base["server"].get("shutdown_timeout_seconds", 30)
                while time.monotonic() < deadline:
                    process.poll()
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.1)
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            metadata.update(stopped_at=utc_now())
            if metadata["status"] == "ready":
                metadata["status"] = "stopped"
            write_json(root / "server.json", metadata)


class GPUSampler:
    def __init__(self, path, interval):
        self.path, self.interval = Path(path), interval
        self.stop = threading.Event()
        self.errors = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        with self.path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "timestamp",
                    "wall_time_utc",
                    "gpu_uuid",
                    "utilization_pct",
                    "memory_used_mib",
                    "power_w",
                ]
            )
            while not self.stop.is_set():
                now = time.time()
                try:
                    data = subprocess.check_output(
                        [
                            "nvidia-smi",
                            "--query-gpu=uuid,utilization.gpu,memory.used,power.draw",
                            "--format=csv,noheader,nounits",
                        ],
                        text=True,
                        timeout=5,
                    )
                    for row in csv.reader(data.splitlines()):
                        writer.writerow([now, timestamp(now)] + [v.strip() for v in row])
                    handle.flush()
                except (OSError, subprocess.SubprocessError) as error:
                    self.errors.append(type(error).__name__)
                self.stop.wait(self.interval)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=10)


def prepare_jobs(config, mixture, rate, repeat, tokenizer, *, duration=None):
    """Independent RNGs preserve the shape sequence when only the rate changes."""
    duration = duration or config["load"]["duration_seconds"]
    seed = config["load"]["seed"] + repeat
    arrivals, shapes = random.Random(seed), random.Random(seed + 100000)
    weights = config["mixtures"][mixture]
    base_ids = tokenizer.encode(
        "Explain request queueing and language model inference using a concrete example. ",
        add_special_tokens=False,
    )
    if not base_ids:
        raise ValueError("tokenizer returned an empty prompt")
    jobs, offset = [], arrivals.expovariate(rate)
    while offset < duration:
        if len(jobs) >= config["load"]["max_requests"]:
            raise ValueError("offered workload exceeds max_requests; shorten the window")
        kind = shapes.choices(list(weights), weights=list(weights.values()))[0]
        shape = config["workloads"][kind]
        shift = len(jobs) % len(base_ids)
        rotated = base_ids[shift:] + base_ids[:shift]
        tokens = (rotated * (1 + shape["prompt_tokens"] // len(rotated)))[: shape["prompt_tokens"]]
        jobs.append(
            {
                "request_id": f"r{len(jobs):06d}",
                "offset": offset,
                "workload": kind,
                "prompt_ids": tokens,
                "output_tokens": shape["output_tokens"],
            }
        )
        offset += arrivals.expovariate(rate)
    if not jobs:
        raise ValueError("empty workload; increase duration or load")
    return jobs


def request_record(url, model, job, origin_wall, origin_mono, timeout, seed):
    started = time.monotonic()
    due = origin_mono + job["offset"]
    record = {
        "request_id": job["request_id"],
        "workload": job["workload"],
        "scheduled_at": origin_wall + job["offset"],
        "started_at": origin_wall + started - origin_mono,
        "scheduled_mono_ns": int(due * 1e9),
        "started_mono_ns": int(started * 1e9),
        "arrival_lag_ms": (started - due) * 1000,
        "prompt_tokens": len(job["prompt_ids"]),
        "output_tokens": job["output_tokens"],
        "prompt_sha256": fingerprint(job["prompt_ids"]),
        "status": "invalid_response",
    }
    payload = {
        "model": model,
        "prompt": job["prompt_ids"],
        "max_tokens": job["output_tokens"],
        "temperature": 0,
        "seed": seed,
        "ignore_eos": True,
        "request_id": job["request_id"],
        "stream_options": {"include_usage": True},
    }
    first = last_content = None
    done, usage, finish = False, None, None
    stream = iter_openai_events(
        stream_sse(url + "/v1/completions", payload, timeout=timeout, max_duration=timeout)
    )
    try:
        for event in stream:
            if event.content:
                last_content = time.monotonic()
                if first is None:
                    first = last_content
            if event.usage:
                usage = event.usage
            if event.finish_reason:
                finish = event.finish_reason
            done |= event.done
        if (
            done
            and first is not None
            and finish == "length"
            and usage
            and usage.get("prompt_tokens") == len(job["prompt_ids"])
            and usage.get("completion_tokens") == job["output_tokens"]
        ):
            record["status"] = "success"
    except TimeoutError:
        record["status"] = "timeout"
    except OpenAIHTTPError as error:
        record.update(status="http_error", http_status=error.status)
    except Exception as error:
        record["error_type"] = type(error).__name__
    finally:
        stream.close()
    end = time.monotonic()
    record.update(
        completed_at=origin_wall + end - origin_mono,
        completed_mono_ns=int(end * 1e9),
        first_content_mono_ns=None if first is None else int(first * 1e9),
        first_content_at=None if first is None else origin_wall + first - origin_mono,
        last_content_at=None if last_content is None else origin_wall + last_content - origin_mono,
        ttft_ms=None if first is None else (first - due) * 1000,
        tpot_ms=(
            None
            if first is None or job["output_tokens"] < 2
            else (last_content - first) * 1000 / (job["output_tokens"] - 1)
        ),
        e2e_ms=(end - due) * 1000,
        usage=usage,
        finish_reason=finish,
    )
    return record


def run_case(
    config,
    base,
    url,
    server,
    group_root,
    mixture,
    rate,
    repeat,
    tokenizer,
    *,
    duration=None,
    tags=None,
):
    duration = duration or config["load"]["duration_seconds"]
    jobs = prepare_jobs(config, mixture, rate, repeat, tokenizer, duration=duration)
    run_id = str(uuid.uuid4())
    root = Path(group_root) / "runs" / run_id
    root.mkdir(parents=True, exist_ok=False)
    metadata = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "running",
        "config": config,
        "config_fingerprint": fingerprint(config),
        "server": server,
        "mixture": mixture,
        "offered_rps": rate,
        "repeat": repeat,
        "tags": tags or {},
        "warmup_start": time.time(),
        "warmup_start_utc": utc_now(),
    }
    write_json(root / "metadata.json", metadata)
    log_start = (Path(group_root) / "server.log").stat().st_size
    try:
        warmups = []
        for i in range(config["load"]["warmup_requests"]):
            job = {**jobs[i % len(jobs)], "offset": 0, "request_id": f"warmup-{run_id}-{i}"}
            warmups.append(
                request_record(
                    url,
                    base["model"]["served_model_name"],
                    job,
                    time.time(),
                    time.monotonic(),
                    config["load"]["request_timeout_seconds"],
                    42,
                )
            )
        write_json(root / "warmup.json", warmups)
        if any(r["status"] != "success" for r in warmups):
            raise RuntimeError("warmup failed")
        before = fetch_text(url + "/metrics")
        write_text(root / "metrics-before.txt", before)
        records = []
        lock = threading.Lock()
        slots = threading.BoundedSemaphore(config["load"]["max_inflight"])
        # Append each completed record immediately; a killed run retains partial evidence.
        with (
            (root / "client.jsonl").open("w") as journal,
            GPUSampler(
                root / "gpu.csv", config["observability"]["gpu_interval_seconds"]
            ) as sampler,
        ):
            origin_wall, origin_mono = time.time(), time.monotonic()
            metadata.update(
                measurement_start=origin_wall,
                measurement_end=origin_wall + duration,
                measurement_start_utc=timestamp(origin_wall),
                measurement_end_utc=timestamp(origin_wall + duration),
            )
            write_json(root / "metadata.json", metadata)

            def persist(record):
                with lock:
                    records.append(record)
                    journal.write(json.dumps(record, allow_nan=False) + "\n")
                    journal.flush()

            def execute(job):
                try:
                    persist(
                        request_record(
                            url,
                            base["model"]["served_model_name"],
                            job,
                            origin_wall,
                            origin_mono,
                            config["load"]["request_timeout_seconds"],
                            42 + repeat,
                        )
                    )
                finally:
                    slots.release()

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=config["load"]["max_inflight"]
            ) as pool:
                futures = []
                for job in jobs:
                    time.sleep(max(0, origin_mono + job["offset"] - time.monotonic()))
                    job = {**job, "request_id": run_id + "-" + job["request_id"]}
                    if slots.acquire(blocking=False):
                        futures.append(pool.submit(execute, job))
                    else:
                        persist(
                            {
                                "request_id": job["request_id"],
                                "workload": job["workload"],
                                "scheduled_at": origin_wall + job["offset"],
                                "status": "client_overflow",
                            }
                        )
                time.sleep(max(0, origin_mono + duration - time.monotonic()))
                for future in futures:
                    future.result()
            metadata["gpu_errors"] = sampler.errors
        write_json(root / "client.json", sorted(records, key=lambda r: r["scheduled_at"]))
        # Allow the last measurement-window scrape to reach Prometheus.
        time.sleep(2 * config["observability"]["scrape_interval_seconds"])
        write_json(root / "prometheus.json", capture(config["observability"], metadata, before))
        write_text(root / "metrics-after.txt", fetch_text(url + "/metrics"))
        metadata.update(status="completed", drain_end=time.time())
    except BaseException as error:
        metadata.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        write_json(root / "metadata.json", metadata)
        with (Path(group_root) / "server.log").open("rb") as log:
            log.seek(log_start)
            write_text(root / "server.log", log.read().decode("utf-8", errors="replace"))
    time.sleep(config["load"]["cooldown_seconds"])
    return root


def load_tokenizer(base, tokenizer=None):
    from transformers import AutoTokenizer

    tokenizer = tokenizer or {"id": base["model"]["id"], "revision": base["model"]["revision"]}
    return AutoTokenizer.from_pretrained(
        tokenizer["id"], revision=tokenizer["revision"], trust_remote_code=False
    )
