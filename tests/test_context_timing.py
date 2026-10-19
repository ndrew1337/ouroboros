"""Section timing of one context build: measured only inside its scope, errors unchanged."""
from __future__ import annotations

import threading

import pytest

from ouroboros.context_timing import measured_sections, section_spans, timed, timed_section


@timed_section("demo")
def _builder(value, *, fail=False):
    if fail:
        raise RuntimeError("the section could not be built")
    return value * 2


def test_a_marked_builder_records_nothing_outside_a_measured_build():
    assert _builder(3) == 6
    assert section_spans() == {}


def test_a_measured_build_sums_each_sections_calls_and_keeps_its_errors():
    with measured_sections():
        assert _builder(1) == 2 and _builder(2) == 4
        assert timed("other", lambda: "built") == "built"
        with pytest.raises(RuntimeError, match="could not be built"):
            _builder(0, fail=True)
        spans = section_spans()
    assert set(spans["sections_ms"]) == set(spans["sections_cpu_ms"]) == {"demo", "other"}
    assert section_spans() == {}


def test_another_threads_build_does_not_see_this_ones_spans():
    seen = {}

    def other():
        _builder(1)
        seen["spans"] = section_spans()

    with measured_sections():
        _builder(1)
        worker = threading.Thread(target=other)
        worker.start()
        worker.join()
        assert set(section_spans()["sections_ms"]) == {"demo"}
    assert seen["spans"] == {}
