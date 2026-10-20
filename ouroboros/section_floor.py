"""Host section views and exact source retention; memory_floor alone selects the steps.

Captures use existing task source handles and their retention/promotion lifecycle,
not a second context-view store or cleanup loop. Unknown producers stay whole.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Mapping, Optional

from ouroboros.context_layout import TIER0_ALWAYS_FULL

log = logging.getLogger(__name__)
PROTECTED = TIER0_ALWAYS_FULL | {"task_contract_discipline", "task_input", "task_authority"}


@dataclass(frozen=True, eq=False, repr=False)
class HostSection(str):
    """A producer's text plus its explicit window view, still usable by string callers.

    The string value is exactly ``full_text``: legacy joins, cache blocks and
    public builders keep their bytes. The frozen fields survive in ContextCore
    for fitting; plain strings enter the capture with no operative view.
    ``reader`` names the original source; relocation always reads the retained
    capture with read_file, so a later change at that source cannot change it.
    """

    section_id: str
    full_text: str
    operative_view: Optional[str] = None
    reader: str = ""

    def __new__(cls, section_id: str, full_text: str, operative_view: Optional[str] = None, reader: str = ""):
        return str.__new__(cls, full_text)

    def __getnewargs__(self):
        return self.section_id, self.full_text, self.operative_view, self.reader


def capture_sections(parts: Iterable[str], *, prefix: str) -> tuple[HostSection, ...]:
    """Keep structural section boundaries without guessing an unknown producer's meaning."""
    return tuple(part if isinstance(part, HostSection) else HostSection(f"{prefix}_{i}", part)
                 for i, part in enumerate(parts))


def labelled_section(section: HostSection, label: Callable[[str], str]) -> HostSection:
    """Apply the same snapshot label to both forms without losing the captured metadata."""
    return replace(section, full_text=label(section.full_text),
                   operative_view=None if section.operative_view is None else label(section.operative_view))


def relocated_text(section: HostSection, reader: str) -> str:
    return (section.operative_view + "\n\n"
            + f"[Host section details omitted for the physical window; retained full text: {reader}.]")


def retain_section(section: HostSection, *, root: str, task_id: str) -> dict:
    """Capture before publishing its pointer; existing source custody owns lifetime."""
    from ouroboros.artifacts import store_actor_source_bytes

    ref = store_actor_source_bytes(root, task_id, category="context_checkpoints",
                                   source_id="host-" + section.section_id,
                                   data=section.full_text.encode("utf-8"), extension="md")
    reader = f"read_file(root='artifact_store', path={ref['path']!r})"
    return {"section_id": section.section_id, "source_ref": ref, "reader": reader}


def section_views(sections: Iterable[HostSection], receipts: Iterable[Mapping]) -> dict[str, str]:
    readers = {row["section_id"]: row["reader"] for row in receipts}
    return {part.section_id: relocated_text(part, readers[part.section_id]) for part in sections
            if part.section_id in readers and part.operative_view is not None}


def render_sections(sections: Iterable[HostSection], fallback: str, addressed: Mapping[str, str]) -> str:
    parts = tuple(sections)
    return "\n\n".join(addressed.get(part.section_id, part.full_text) for part in parts) if parts else fallback
