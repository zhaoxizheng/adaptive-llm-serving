from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence

from src.workload import TraceRequest, validate_trace


class PolicyName(StrEnum):
    # 不组批立即派发；固定窗口到边界派发；size_or_time 达到批量或最老请求期限即派发。
    NO_BATCHING = "no_batching"
    FIXED_WINDOW = "fixed_window"
    SIZE_OR_TIME = "size_or_time"


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    policy: PolicyName | str
    max_batch_size: int = 1
    window_ns: int = 0
    max_wait_ns: int = 0
    queue_capacity: int = 128
    admission_timeout_ns: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy", PolicyName(self.policy))
        if self.max_batch_size <= 0:
            raise ValueError("max_batch_size must be positive")
        if self.queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        if self.window_ns < 0 or self.max_wait_ns < 0:
            raise ValueError("batch delays must be nonnegative")
        if self.admission_timeout_ns < 0:
            raise ValueError("admission_timeout_ns must be nonnegative")
        if self.policy is PolicyName.NO_BATCHING and self.max_batch_size != 1:
            raise ValueError("no_batching requires max_batch_size=1")
        if self.policy is PolicyName.FIXED_WINDOW and self.window_ns <= 0:
            raise ValueError("fixed_window requires window_ns > 0")


@dataclass(frozen=True, slots=True)
class QueuedRequest:
    request: TraceRequest
    observed_arrival_ns: int
    admitted_ns: int


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    request: TraceRequest
    accepted: bool
    observed_arrival_ns: int
    admitted_ns: int | None
    terminal_ns: int | None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Batch:
    batch_id: str
    dispatch_ns: int
    requests: tuple[QueuedRequest, ...]
    trigger: str

    @property
    def request_ids(self) -> tuple[str, ...]:
        return tuple(item.request.request_id for item in self.requests)


class VirtualTimeScheduler:
    """由调用方显式推进时间的纯请求级组批状态机，不拥有线程、时钟或模型。

    调用方应先提交时间 t 的所有到达，再调用 advance_to(t)。因此恰好在窗口边界
    或最老请求期限到达的请求，也能加入该时刻派发的批次。时间单位为纳秒。
    """

    def __init__(self, config: SchedulerConfig):
        self.config = config
        self._queue: deque[QueuedRequest] = deque()
        self._now_ns = 0
        self._batch_ordinal = 0
        self._closed = False
        self._next_window_ns = config.window_ns if config.window_ns else None

    @property
    def now_ns(self) -> int:
        return self._now_ns

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    @property
    def closed(self) -> bool:
        return self._closed

    def _move_time(self, now_ns: int) -> None:
        if now_ns < self._now_ns:
            raise ValueError(
                f"virtual time cannot move backwards: {now_ns} < {self._now_ns}"
            )
        self._now_ns = now_ns

    def submit(
        self,
        request: TraceRequest,
        now_ns: int | None = None,
        *,
        observed_arrival_ns: int | None = None,
    ) -> AdmissionDecision:
        if self._closed:
            raise ValueError("cannot submit after scheduler shutdown")
        timestamp = request.scheduled_arrival_ns if now_ns is None else now_ns
        self._move_time(timestamp)
        observed = timestamp if observed_arrival_ns is None else observed_arrival_ns
        if observed < request.scheduled_arrival_ns or observed > timestamp:
            raise ValueError(
                "observed_arrival_ns must be between scheduled arrival and admission attempt"
            )
        # 容量只针对待组批队列；满时返回拒绝及终态时间，不在此睡眠或阻塞等待。
        if len(self._queue) >= self.config.queue_capacity:
            terminal = timestamp + self.config.admission_timeout_ns
            return AdmissionDecision(
                request=request,
                accepted=False,
                observed_arrival_ns=observed,
                admitted_ns=None,
                terminal_ns=terminal,
                reason="queue_capacity_timeout",
            )
        self._queue.append(
            QueuedRequest(
                request=request, observed_arrival_ns=observed, admitted_ns=timestamp
            )
        )
        return AdmissionDecision(
            request=request,
            accepted=True,
            observed_arrival_ns=observed,
            admitted_ns=timestamp,
            terminal_ns=None,
        )

    def next_wakeup_ns(self) -> int | None:
        """返回策略下次应检查派发的时间；由外层事件循环负责唤醒。"""
        if not self._queue or self._closed:
            return None
        if self.config.policy is PolicyName.NO_BATCHING:
            return self._now_ns
        if self.config.policy is PolicyName.FIXED_WINDOW:
            assert self._next_window_ns is not None
            oldest = self._queue[0].admitted_ns
            first_eligible = (
                (oldest + self.config.window_ns - 1) // self.config.window_ns
            ) * self.config.window_ns
            return max(self._next_window_ns, first_eligible)
        if len(self._queue) >= self.config.max_batch_size:
            return self._now_ns
        return self._queue[0].admitted_ns + self.config.max_wait_ns

    def _eligible_dispatch(self, now_ns: int) -> tuple[int, str] | None:
        if not self._queue:
            return None
        if self.config.policy is PolicyName.NO_BATCHING:
            return now_ns, "immediate"
        if self.config.policy is PolicyName.FIXED_WINDOW:
            wakeup = self.next_wakeup_ns()
            if wakeup is None or wakeup > now_ns:
                return None
            return wakeup, "window"
        if len(self._queue) >= self.config.max_batch_size:
            return now_ns, "size"
        # size_or_time 未凑满时按队首的准入时间计算期限，新请求不会重置最老请求的等待。
        deadline = self._queue[0].admitted_ns + self.config.max_wait_ns
        if deadline <= now_ns:
            return deadline, "timeout"
        return None

    def _pop_batch(self, dispatch_ns: int, trigger: str) -> Batch:
        # 按 FIFO 取不超过 max_batch_size 个请求，仅形成批次，不执行模型。
        count = min(self.config.max_batch_size, len(self._queue))
        items = tuple(self._queue.popleft() for _ in range(count))
        batch = Batch(
            batch_id=f"batch-{self._batch_ordinal:06d}",
            dispatch_ns=dispatch_ns,
            requests=items,
            trigger=trigger,
        )
        self._batch_ordinal += 1
        if self.config.policy is PolicyName.FIXED_WINDOW:
            self._next_window_ns = dispatch_ns + self.config.window_ns
        return batch

    def advance_to(
        self, now_ns: int, *, dispatch_slots: int | None = None
    ) -> tuple[Batch, ...]:
        """推进虚拟时间并返回可派发批次，以 dispatch_slots 表达 worker 背压。

        None 表示派发所有满足策略的批次；0 只推进时间，不形成待执行批次。
        """

        self._move_time(now_ns)
        if dispatch_slots is not None and dispatch_slots < 0:
            raise ValueError("dispatch_slots must be nonnegative or None")
        if self._closed:
            return ()
        if dispatch_slots == 0:
            # 单 worker 忙时没有派发槽位；跨过的固定窗口跳过，不能事后伪造历史派发。
            if (
                self.config.policy is PolicyName.FIXED_WINDOW
                and self._next_window_ns is not None
                and self._next_window_ns <= now_ns
            ):
                self._next_window_ns = (
                    (now_ns // self.config.window_ns) + 1
                ) * self.config.window_ns
            return ()
        batches: list[Batch] = []
        while dispatch_slots is None or len(batches) < dispatch_slots:
            eligible = self._eligible_dispatch(now_ns)
            if eligible is None:
                break
            dispatch_ns, trigger = eligible
            # A caller that jumps over a wakeup receives the historical policy
            # timestamp. A real adapter should always step through wakeups.
            batches.append(self._pop_batch(dispatch_ns, trigger))
            if self.config.policy is PolicyName.SIZE_OR_TIME:
                # A partial oldest-timeout batch drains the whole queue. A full
                # queue may immediately yield more size-triggered batches.
                continue
        return tuple(batches)

    def close(
        self, now_ns: int, *, dispatch_slots: int | None = None
    ) -> tuple[Batch, ...]:
        """关闭时按 FIFO 清空已准入请求，可受 worker 槽位限制分次派发。"""

        self._move_time(now_ns)
        if self._closed:
            return ()
        if dispatch_slots is not None and dispatch_slots < 0:
            raise ValueError("dispatch_slots must be nonnegative or None")
        batches: list[Batch] = []
        while self._queue and (
            dispatch_slots is None or len(batches) < dispatch_slots
        ):
            batches.append(self._pop_batch(now_ns, "shutdown"))
        self._closed = not self._queue
        return tuple(batches)


def build_scheduler(config: SchedulerConfig) -> VirtualTimeScheduler:
    return VirtualTimeScheduler(config)


def schedule_trace(
    trace: Sequence[TraceRequest], config: SchedulerConfig
) -> tuple[Batch, ...]:
    """生成确定性组批示例，不模拟 worker 服务耗时或背压。

    假设请求立即准入且派发不受限；不能用它代替真实服务性能测试。
    """

    validate_trace(trace)
    scheduler = VirtualTimeScheduler(config)
    batches: list[Batch] = []
    for request in trace:
        wakeup = scheduler.next_wakeup_ns()
        while wakeup is not None and wakeup < request.scheduled_arrival_ns:
            batches.extend(scheduler.advance_to(wakeup))
            wakeup = scheduler.next_wakeup_ns()
        decision = scheduler.submit(request, request.scheduled_arrival_ns)
        if not decision.accepted:
            raise ValueError(
                "schedule_trace queue overflowed; use the serving simulation for rejection semantics"
            )
        batches.extend(scheduler.advance_to(request.scheduled_arrival_ns))
    wakeup = scheduler.next_wakeup_ns()
    while wakeup is not None:
        batches.extend(scheduler.advance_to(wakeup))
        wakeup = scheduler.next_wakeup_ns()
    batches.extend(scheduler.close(scheduler.now_ns))
    return tuple(batches)
