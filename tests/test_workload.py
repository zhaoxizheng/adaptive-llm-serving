from __future__ import annotations

from src.workload import generate_poisson_trace, read_trace, trace_fingerprint, write_trace


def generate(seed: int = 42):
    return generate_poisson_trace(
        arrival_rate_rps=20,
        duration_seconds=1,
        seed=seed,
        prompt_tokens=256,
        output_tokens=64,
    )


def test_poisson_trace_is_seeded_stable_and_ordered() -> None:
    left = generate()
    right = generate()

    assert left == right
    assert left != generate(43)
    assert [item.request_id for item in left] == [f"req-{index:06d}" for index in range(len(left))]
    assert [item.scheduled_arrival_ns for item in left] == sorted(item.scheduled_arrival_ns for item in left)


def test_trace_round_trip_is_byte_stable(tmp_path) -> None:
    trace = generate()
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"

    trace_id = write_trace(first, trace)
    restored = read_trace(first)
    write_trace(second, restored)

    assert trace_id == trace_fingerprint(trace)
    assert restored == trace
    assert first.read_bytes() == second.read_bytes()


def test_arrival_stream_is_independent_of_mixed_shape_draws() -> None:
    fixed = generate_poisson_trace(
        arrival_rate_rps=10, duration_seconds=2, seed=7, prompt_tokens=256, output_tokens=64
    )
    mixed = generate_poisson_trace(
        arrival_rate_rps=10,
        duration_seconds=2,
        seed=7,
        prompt_tokens=256,
        output_tokens=64,
        workload_classes=[
            {"name": "short", "weight": 1, "prompt_tokens": 128, "output_tokens": 32},
            {"name": "long", "weight": 1, "prompt_tokens": 1024, "output_tokens": 128},
        ],
    )

    assert [item.scheduled_arrival_ns for item in fixed] == [item.scheduled_arrival_ns for item in mixed]
