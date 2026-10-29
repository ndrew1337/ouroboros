"""``record_update_description``: the one writer of the update description's prose.

The mind writes the description as ordinary work — on the owner's request from the Updates
panel or on its own initiative — after reading the range (``query_data`` kind
``update_material``, ``vcs_read``). This tool saves it at the installation's canonical record
(``update_letter.save_description``) under the record's cross-process lock, with authorship
the host derives, never the caller: this task, its live model, and whether it is the owner's
recorded request. A save that would erase a newer description, or label text with a range
nobody offered, is refused with what is there instead. Prose only: no apply, settings or
restart authority.

After a save, the existing ``update_progress_changed`` frame asks open windows to read the
status again: put on the turn's event queue (the supervisor broadcasts it as it drains), or
handed to a running server loop when the call already runs in that process.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.tool_result import ToolResult, _publish_tool_result

log = logging.getLogger(__name__)

_DESCRIPTION = (
    "Save your description of an official update for your human: Dashboard → Updates and the Apply "
    "confirmation show it, and it stays after the update lands. Read the range first: query_data(op='json', "
    "kind='update_material', select='text', max_bytes=<its text_bytes>), and vcs_read(root='system_repo', "
    "action='show'|'diff') "
    "where a message does not say what changed. text is Markdown in your human's language, as long and as "
    "structured as the change needs. base_sha/target_sha name the exact range the text describes: the range "
    "the last check offered, the saved description's own range, or the range of your human's request "
    "(query_data(op='json', kind='update_letter'): checked_key, key, request.key). expected_revision is that "
    "record's revision (0 when it has none); a newer save is refused, never overwritten, so read it and decide."
)


def _record_update_description(ctx: ToolContext, text: str = "", base_sha: str = "", target_sha: str = "",
                               expected_revision: Any = None, **_ignored: Any) -> str:
    from ouroboros.tool_access import canonical_data_root
    from ouroboros.update_letter import RecordBusy, save_description

    author = {"task_id": str(getattr(ctx, "task_id", "") or ""), "model": str(getattr(ctx, "active_model", "") or "")}
    try:
        outcome = save_description(text, str(base_sha or "").strip().lower(), str(target_sha or "").strip().lower(),
                                   expected_revision, author=author, drive_root=canonical_data_root(ctx))
    except ValueError as exc:
        return _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR", text=f"ERROR: {exc}"))
    except RecordBusy as exc:
        return _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ERROR",
                                                    text=f"ERROR: {exc}; nothing was saved. Try again."))
    if outcome["outcome"] == "revision_conflict":
        return _publish_tool_result(ctx, ToolResult(status="blocked", code="RESOURCE_CONSTRAINT_BLOCKED", text=(
            f"⚠️ RESOURCE_CONSTRAINT_BLOCKED: revision conflict — the saved description is at revision "
            f"{outcome['revision']}, not {expected_revision}; nothing was saved. The newer one stays: read it and decide.\n"
            + json.dumps(outcome, ensure_ascii=False))))
    if outcome["outcome"] == "range_not_offered":
        return _publish_tool_result(ctx, ToolResult(status="blocked", code="RESOURCE_CONSTRAINT_BLOCKED", text=(
            "⚠️ RESOURCE_CONSTRAINT_BLOCKED: range not offered — no check, saved description or request names "
            "this range; nothing was saved.\n" + json.dumps(outcome, ensure_ascii=False))))
    outcome["window_refresh"] = _hand_on_refresh(ctx)
    return json.dumps({"ok": True, **outcome}, ensure_ascii=False)


def _hand_on_refresh(ctx: ToolContext) -> str:
    """Hand ``update_progress_changed`` on and name the handoff: ``queued_for_host``,
    ``handed_to_server_loop`` or ``not_sent``. None confirms that a window read again."""
    event_queue = getattr(ctx, "event_queue", None)
    try:
        if event_queue is not None:
            event_queue.put_nowait({"type": "update_progress_changed"})
            return "queued_for_host"
        from ouroboros.gateway.ws import broadcast_ws_sync

        if broadcast_ws_sync({"type": "update_progress_changed"}):
            return "handed_to_server_loop"
    except Exception:
        log.debug("update_progress_changed not handed on", exc_info=True)
    return "not_sent"


def update_description_tool_entries() -> List[ToolEntry]:
    parameters: Dict[str, Any] = {"type": "object", "required": ["text", "base_sha", "target_sha", "expected_revision"],
                                  "additionalProperties": False, "properties": {
        "text": {"type": "string", "description": "The description, Markdown."},
        "base_sha": {"type": "string", "description": "Full SHA of the range's base (the running source)."},
        "target_sha": {"type": "string", "description": "Full SHA of the range's official target."},
        "expected_revision": {"type": "integer", "minimum": 0,
                              "description": "The revision of the record you read; 0 when it has none."},
    }}
    return [ToolEntry("record_update_description", {"name": "record_update_description", "description": _DESCRIPTION,
                                                    "parameters": parameters},
                      _record_update_description, timeout_sec=60, safety_policy="skip")]
