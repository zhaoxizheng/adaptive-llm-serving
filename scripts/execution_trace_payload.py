"""Copied to vllm/_study_execution.py; torch is imported only when enabled."""

import contextlib
import contextvars
import functools
import json
import os
import threading
import time
from pathlib import Path

_STEP = contextvars.ContextVar("execution_step", default=-1)
_CAPTURE = None
_COUNT = 0


def device_metric(item, field):
    """Torch renamed cuda_* aggregates to device_*; require one documented field."""
    if hasattr(item, field):
        return getattr(item, field)
    legacy = field.replace("device", "cuda")
    if hasattr(item, legacy):
        return getattr(item, legacy)
    raise ValueError(f"profiler aggregate lacks {field} / {legacy}")


def enabled():
    return bool(os.environ.get("VLLM_EXECUTION_CONFIG"))


def config():
    return json.loads(os.environ["VLLM_EXECUTION_CONFIG"])


def emit(event, **fields):
    global _COUNT
    if not enabled():
        return
    cfg = config()
    if _COUNT > cfg.get("event_limit", 100000):
        return
    if _COUNT == cfg.get("event_limit", 100000):
        event, fields = "trace_truncated", {}
    root = Path(cfg["output"])
    root.mkdir(parents=True, exist_ok=True)
    record = dict(
        schema_version=1,
        component="execution",
        event=event,
        step=_STEP.get(),
        pid=os.getpid(),
        thread=threading.get_ident(),
        ts_ns=time.monotonic_ns(),
        **fields,
    )
    with (root / f"execution-{os.getpid()}.jsonl").open("a") as handle:
        handle.write(json.dumps(record, allow_nan=False) + "\n")
    _COUNT += 1


@contextlib.contextmanager
def phase(name):
    if not enabled():
        yield
        return
    import torch

    label = f"study/{name}/step={_STEP.get()}"
    start = time.monotonic_ns()
    with torch.profiler.record_function(label), torch.cuda.nvtx.range(label):
        yield
    emit(
        "boundary", phase=name, start_ns=start, cpu_duration_us=(time.monotonic_ns() - start) / 1000
    )


def boundary(name):
    def decorate(method):
        @functools.wraps(method)
        def wrapped(*args, **kwargs):
            with phase(name):
                return method(*args, **kwargs)

        return wrapped

    return decorate


def schedule(self, output):
    if enabled():
        self._deep_step = getattr(self, "_deep_step", -1) + 1
        output.study_step = self._deep_step
        token = _STEP.set(self._deep_step)
        try:
            rows = []
            for rid, count in output.num_scheduled_tokens.items():
                request = self.requests[rid]
                rows.append(
                    dict(
                        request_id=rid,
                        tokens=count,
                        computed_tokens=request.num_computed_tokens,
                        prompt_tokens=request.num_prompt_tokens,
                    )
                )
            emit("schedule", scheduled=rows)
        finally:
            _STEP.reset(token)


def scheduler(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        if not enabled():
            return method(self, *args, **kwargs)
        token = _STEP.set(getattr(self, "_deep_step", -1) + 1)
        try:
            with phase("scheduler"):
                return method(self, *args, **kwargs)
        finally:
            _STEP.reset(token)

    return wrapped


def shape(runner, output, input_ids, positions, num_input_tokens, mode):
    if enabled():
        prefill = 0
        for rid, count in output.num_scheduled_tokens.items():
            state = runner.requests[rid]
            prefill += min(count, max(0, state.num_prompt_tokens - state.num_computed_tokens))
        total = output.total_num_scheduled_tokens
        emit(
            "shape",
            request_count=len(output.num_scheduled_tokens),
            scheduled_tokens=total,
            prefill_tokens=prefill,
            decode_tokens=total - prefill,
            input_ids_shape=list(input_ids.shape),
            positions_shape=list(positions.shape),
            padded_tokens=num_input_tokens,
            kv_slot_count=total,
            dispatch_mode=mode.name,
            gpu_execute_us=None,
        )


def graph(event):
    if enabled():
        emit(event)


class Capture:
    """A single worker-local bounded schedule, armed after external model warmup."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.index = 0
        self.profiler = None
        self.started = False
        self.done = False
        self.steps = []

    def ready(self):
        return not self.done and Path(self.cfg["arm_file"]).is_file()

    def before(self):
        if not self.ready():
            return False
        import torch

        cfg = self.cfg
        if not self.started:
            self.started = True
            if cfg["mode"] == "torch":
                self.profiler = torch.profiler.profile(
                    activities=[
                        torch.profiler.ProfilerActivity.CPU,
                        torch.profiler.ProfilerActivity.CUDA,
                    ],
                    schedule=torch.profiler.schedule(
                        wait=cfg["wait"], warmup=cfg["warmup"], active=cfg["active"], repeat=1
                    ),
                    record_shapes=cfg["record_shapes"],
                    profile_memory=cfg["profile_memory"],
                    with_stack=cfg["with_stack"],
                    on_trace_ready=self.export,
                )
                self.profiler.start()
        active = self.index >= cfg["wait"] + cfg["warmup"]
        if cfg["mode"] == "nsys" and self.index == cfg["wait"] + cfg["warmup"]:
            torch.cuda.synchronize()
            torch.cuda.profiler.start()
        if active:
            self.steps.append(_STEP.get())
        emit("capture_step", capture_index=self.index, active=active)
        return True

    def after(self):
        import torch

        self.index += 1
        if self.profiler is not None:
            self.profiler.step()
        if self.index == self.cfg["wait"] + self.cfg["warmup"] + self.cfg["active"]:
            if self.profiler is not None:
                self.profiler.stop()
            else:
                torch.cuda.synchronize()
                torch.cuda.profiler.stop()
            self.done = True
            root = Path(self.cfg["output"])
            (root / f"capture-{os.getpid()}.json").write_text(
                json.dumps(
                    dict(complete=True, active_steps=self.steps, config=self.cfg, pid=os.getpid())
                )
            )

    def export(self, profiler):
        root = Path(self.cfg["output"])
        root.mkdir(parents=True, exist_ok=True)
        profiler.export_chrome_trace(str(root / f"torch-{os.getpid()}.json"))
        rows = []
        for item in profiler.key_averages(group_by_input_shape=self.cfg["record_shapes"]):
            rows.append(
                dict(
                    name=item.key,
                    count=item.count,
                    input_shapes=item.input_shapes,
                    self_cpu_us=item.self_cpu_time_total,
                    cpu_total_us=item.cpu_time_total,
                    self_device_us=device_metric(item, "self_device_time_total"),
                    device_total_us=device_metric(item, "device_time_total"),
                    self_cpu_memory_bytes=item.self_cpu_memory_usage,
                    self_device_memory_bytes=device_metric(item, "self_device_memory_usage"),
                )
            )
        (root / f"operators-{os.getpid()}.json").write_text(json.dumps(rows))


def execution(method):
    @functools.wraps(method)
    def wrapped(self, scheduler_output, *args, **kwargs):
        global _CAPTURE
        if not enabled():
            return method(self, scheduler_output, *args, **kwargs)
        token = _STEP.set(scheduler_output.study_step)
        cfg = config()
        if cfg["mode"] in {"torch", "nsys"} and _CAPTURE is None:
            _CAPTURE = Capture(cfg)
        capturing = False
        try:
            if scheduler_output.total_num_scheduled_tokens and _CAPTURE is not None:
                capturing = _CAPTURE.before()
            with phase("execute"):
                result = method(self, scheduler_output, *args, **kwargs)
            emit("output", scheduled_tokens=scheduler_output.total_num_scheduled_tokens)
            if capturing:
                _CAPTURE.after()
            return result
        finally:
            _STEP.reset(token)

    return wrapped
