"""Owner-approved behavioral clauses survive prompt compression."""
from pathlib import Path

import pytest

SYSTEM = Path(__file__).resolve().parents[1] / "prompts/SYSTEM.md"


def _contains(text, clause):
    assert clause in " ".join(text.split())


def test_absent_owner_client_rule_is_available_without_a_runtime_client_note():
    clause = "when `owner_client` is absent I ask or hedge rather than assume a browser"
    section = SYSTEM.read_text().split("## Environment and My Human", 1)[1].split("## Drift Detector", 1)[0]
    _contains(section, clause)
    with pytest.raises(AssertionError):
        _contains(" ".join(section.split()).replace(clause, ""), clause)
