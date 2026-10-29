"""Reversible project closure metadata, list selection and accepted owner-message reopening.

Closure is the mind's or owner's recorded decision, separate from routing lifecycle.
The registry's existing writer lock serializes changes with bindings and deletion.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def apply_closed(project: dict, value: dict | None) -> None:
    """Apply a complete closure record, or remove it to reopen; never change lifecycle."""
    if value is None:
        project.pop("closed", None)
        return
    if (not isinstance(value, dict) or set(value) != {"at", "by", "note"}
            or any(not isinstance(value[key], str) for key in ("at", "by", "note"))
            or not value["at"].strip() or not value["by"].strip()):
        raise ValueError("closed must be None or {at, by, note} with string values and nonempty at/by")
    project["closed"] = dict(value)


def select_projects(projects, *, closed: bool | None = None, oldest_first: bool = False) -> list[dict]:
    """Filter before limiting; recency comes from the caller's captured room facts."""
    if closed is not None and type(closed) is not bool:
        raise ValueError("closed must be a boolean or None")
    if type(oldest_first) is not bool:
        raise ValueError("oldest_first must be a boolean")
    rows = [p for p in projects if closed is None or ("closed" in p) == closed]
    return sorted(rows, key=lambda p: (str(p.get("last_active_at") or ""), str(p.get("id") or "")),
                  reverse=not oldest_first)


def change_project_closed(drive_root, project_id: str, value: dict | None) -> tuple[dict | None, bool]:
    """Change an active room once; an already open/closed room keeps its original decision."""
    from ouroboros import projects_registry as registry
    from ouroboros.project_facts import sanitize_project_id

    pid = sanitize_project_id(project_id)
    if not pid:
        return None, False
    with registry._file_write_lock(registry._registry_path(drive_root)):
        data = registry._load(drive_root, strict=True, project_id=pid)
        for row in data["projects"]:
            if row.get("id") != pid or row.get("lifecycle") != registry.PROJECT_ACTIVE:
                continue
            if ("closed" in row) == (value is not None):
                return dict(row), False
            apply_closed(row, value)
            registry._save(drive_root, data)
            return dict(row), True
    return None, False


def reopen_for_owner_message(drive_root, row: dict) -> None:
    """A newly accepted local owner row reopens its room; skill/host rows carry no such choice.

    The local owner door fixes user_id=1; the selected positive chat_id is the room.
    Called only after a fresh append, so replay of an old message cannot undo a later closure.
    A registry failure never changes the already durable message's acceptance receipt.
    """
    if row.get("direction") != "in" or row.get("source") != "web" or row.get("user_id") != 1:
        return
    from ouroboros.projects_registry import PROJECT_ACTIVE, list_reserved_projects

    try:
        project = next((p for p in list_reserved_projects(drive_root, strict=True)
                        if p.get("chat_id") == row.get("chat_id") and p.get("lifecycle") == PROJECT_ACTIVE), None)
        if project and "closed" in project:
            change_project_closed(drive_root, project["id"], None)
    except Exception:
        log.warning("Accepted owner message could not reopen project room %s", row.get("chat_id"), exc_info=True)
