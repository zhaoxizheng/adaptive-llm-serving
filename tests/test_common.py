import pytest

from src.metrics import percentile
from src.result_store import append_row, case_key, read_rows


def test_percentile_uses_nearest_rank() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 0.50) == 30.0
    assert percentile(values, 0.95) == 50.0


def test_percentile_empty_values() -> None:
    assert percentile([], 0.95) == 0.0


def test_percentile_rejects_invalid_fraction() -> None:
    with pytest.raises(ValueError):
        percentile([1.0], 1.1)


def test_result_store_appends_and_resumes(tmp_path) -> None:
    output = tmp_path / "results.csv"
    fields = ["prompt_tokens", "output_tokens", "repeat", "use_cache", "value"]
    first = {
        "prompt_tokens": 32,
        "output_tokens": 8,
        "repeat": 0,
        "use_cache": True,
        "value": 1.5,
    }
    second = {**first, "use_cache": False, "value": 9.5}

    append_row(output, first, fields)
    append_row(output, second, fields)
    rows = read_rows(output)

    assert len(rows) == 2
    assert case_key(rows[0]) == (32, 8, 0, True)
    assert case_key(rows[1]) == (32, 8, 0, False)
