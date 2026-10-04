from __future__ import annotations

from src.dynamic_batcher import SchedulerConfig, VirtualTimeScheduler, schedule_trace
from src.workload import TraceRequest

MS = 1_000_000


def request(index: int, milliseconds: int) -> TraceRequest:
    return TraceRequest(index, f"r{index}", milliseconds * MS, 256, 64)


def test_no_batching_dispatches_fifo_singletons_immediately() -> None:
    batches = schedule_trace(
        [request(0, 1), request(1, 1)],
        SchedulerConfig("no_batching", max_batch_size=1),
    )

    assert [(batch.request_ids, batch.dispatch_ns) for batch in batches] == [
        (("r0",), MS),
        (("r1",), MS),
    ]


def test_fixed_window_is_anchored_to_zero_and_boundary_arrival_joins() -> None:
    batches = schedule_trace(
        [request(0, 1), request(1, 5), request(2, 6)],
        SchedulerConfig("fixed_window", max_batch_size=4, window_ns=5 * MS),
    )

    assert [(batch.request_ids, batch.dispatch_ns) for batch in batches] == [
        (("r0", "r1"), 5 * MS),
        (("r2",), 10 * MS),
    ]


def test_size_or_time_golden_example_and_oldest_deadline() -> None:
    batches = schedule_trace(
        [request(0, 0), request(1, 2), request(2, 5), request(3, 8)],
        SchedulerConfig("size_or_time", max_batch_size=3, max_wait_ns=5 * MS),
    )

    assert [(batch.request_ids, batch.dispatch_ns, batch.trigger) for batch in batches] == [
        (("r0", "r1", "r2"), 5 * MS, "size"),
        (("r3",), 13 * MS, "timeout"),
    ]


def test_shutdown_flushes_partial_batch_and_is_idempotent() -> None:
    scheduler = VirtualTimeScheduler(
        SchedulerConfig("size_or_time", max_batch_size=4, max_wait_ns=10 * MS)
    )
    scheduler.submit(request(0, 1), MS)

    flushed = scheduler.close(2 * MS)

    assert flushed[0].request_ids == ("r0",)
    assert flushed[0].trigger == "shutdown"
    assert scheduler.close(2 * MS) == ()
