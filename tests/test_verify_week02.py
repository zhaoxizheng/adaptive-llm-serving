from __future__ import annotations

import pytest

from scripts.verify_week02 import parse_report_evidence


def test_parse_report_evidence_requires_unique_well_formed_keys() -> None:
    block = """<!-- WEEK02-EVIDENCE
run_id=abc
raw_sha256=def
-->"""
    assert parse_report_evidence(block) == {"run_id": "abc", "raw_sha256": "def"}

    with pytest.raises(ValueError, match="malformed"):
        parse_report_evidence("<!-- WEEK02-EVIDENCE\nkey=a\nkey=b\n-->")
    with pytest.raises(ValueError, match="missing"):
        parse_report_evidence("no evidence")
