"""Wall and thread CPU time of each host section of one context build, for that build's own receipt.

A task's or a call's start is mostly the host sections its context reads, one after another. A builder
marked ``timed_section`` (or called through ``timed``) adds its durations to the spans of the build
measured on this thread of control (a context variable the agent opens around ``build_llm_messages``);
the ``context_memory_view`` receipt in ``logs/events.jsonl`` carries them as ``sections_ms`` and
``sections_cpu_ms``. Wall time far above CPU time is waiting (a lock, the disk, the interpreter's other
threads), so a slow start is read from the log instead of sampled. Spans are inclusive and only for the
marked builders. Outside a measured build a marked builder costs one variable lookup and records nothing.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import time
from typing import Any, Callable, Dict, Iterator, List, Optional

_SPANS: contextvars.ContextVar[Optional[Dict[str, List[float]]]] = contextvars.ContextVar("context_section_spans", default=None)


def timed_section(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Add the builder's wall and thread CPU time under ``name`` to the build being measured, if any."""
    def decorate(builder: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(builder)
        def measured(*args: Any, **kwargs: Any) -> Any:
            spans = _SPANS.get()
            if spans is None:
                return builder(*args, **kwargs)
            wall, cpu = time.perf_counter(), time.thread_time()
            try:
                return builder(*args, **kwargs)
            finally:
                span = spans.setdefault(name, [0.0, 0.0])
                span[0] += time.perf_counter() - wall
                span[1] += time.thread_time() - cpu
        return measured
    return decorate


def timed(name: str, builder: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """``builder(*args, **kwargs)`` measured under ``name``, for a builder whose module does not import this one."""
    return timed_section(name)(builder)(*args, **kwargs)


@contextlib.contextmanager
def measured_sections() -> Iterator[None]:
    """Measure the sections of the context build inside this block."""
    token = _SPANS.set({})
    try:
        yield
    finally:
        _SPANS.reset(token)


def section_spans() -> Dict[str, Dict[str, int]]:
    """``sections_ms`` and ``sections_cpu_ms`` of the build being measured, or nothing outside one."""
    spans = _SPANS.get()
    if not spans:
        return {}
    return {"sections_ms": {name: round(span[0] * 1000) for name, span in spans.items()},
            "sections_cpu_ms": {name: round(span[1] * 1000) for name, span in spans.items()}}
