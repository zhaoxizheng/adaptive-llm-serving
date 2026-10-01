from __future__ import annotations

import pytest

from src.result_store import append_row, read_rows

FIELDS = ["prompt_tokens", "output_tokens", "repeat", "use_cache", "value"]


def test_read_rows_rejects_duplicate_header_fields(tmp_path) -> None:
    path = tmp_path / "results.csv"
    path.write_text(
        "prompt_tokens,output_tokens,repeat,use_cache,repeat\n32,8,0,true,0\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"duplicate fields: \['repeat'\]"):
        read_rows(path)


@pytest.mark.parametrize("values", ["32,8,0,true", "32,8,0,true,1.5,extra"])
def test_read_rows_rejects_short_and_long_records(tmp_path, values: str) -> None:
    path = tmp_path / "results.csv"
    path.write_text(",".join(FIELDS) + "\n" + values + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Malformed CSV row 2"):
        read_rows(path)


def test_read_rows_requires_the_exact_result_schema(tmp_path) -> None:
    path = tmp_path / "results.csv"
    reordered = ["output_tokens", "prompt_tokens", "repeat", "use_cache", "value"]
    path.write_text(",".join(reordered) + "\n8,32,0,true,1.5\n", encoding="utf-8")

    with pytest.raises(ValueError, match="header does not match the result schema"):
        read_rows(path, expected_fields=FIELDS)


@pytest.mark.parametrize(
    "row",
    [
        {
            "prompt_tokens": 32,
            "output_tokens": 8,
            "repeat": 0,
            "use_cache": True,
        },
        {
            "prompt_tokens": 32,
            "output_tokens": 8,
            "repeat": 0,
            "use_cache": True,
            "value": 1.5,
            "unexpected": "field",
        },
    ],
)
def test_append_row_rejects_missing_or_extra_fields(tmp_path, row: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="Result row schema mismatch"):
        append_row(tmp_path / "results.csv", row, FIELDS)


def test_append_row_preserves_an_existing_malformed_file(tmp_path) -> None:
    path = tmp_path / "results.csv"
    malformed = ",".join(FIELDS) + "\n32,8,0,true\n"
    path.write_text(malformed, encoding="utf-8")
    row = {
        "prompt_tokens": 32,
        "output_tokens": 8,
        "repeat": 0,
        "use_cache": True,
        "value": 1.5,
    }

    with pytest.raises(ValueError, match="Malformed CSV row 2"):
        append_row(path, row, FIELDS)

    assert path.read_text(encoding="utf-8") == malformed
