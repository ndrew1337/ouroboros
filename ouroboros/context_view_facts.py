"""Names and sizes of the selected context, read from its captured sources."""
from __future__ import annotations

from ouroboros import memory_floor as floor, memory_view as mv


def memory_snapshot(plan):
    core = getattr(plan, "core", None)
    return mv.snapshot_from_json(core.memory_view_json) if core and core.memory_view_json else None


def block_label(message, index, block, plan):
    text = block.get("text", "") if isinstance(block, dict) else str(block)
    if message.get("role") != "system":
        return "assignment" if index == 1 else message.get("role", "message")
    if "## My story" in text:
        return "my story and identity"
    if "## This room" in text or "## Live rooms" in text:
        return "this room, live rooms and runtime facts"
    if text.startswith("## DEVELOPMENT.md"):
        return "development book"
    return "core and books" if index == 0 else "runtime facts"


def wake_facts(plan, mode):
    snapshot = memory_snapshot(plan)
    if snapshot is None or snapshot.spec.role != "consciousness":
        return ""
    rooms = ([snapshot.room] if snapshot.room else []) + list(snapshot.live_rooms)
    chars = sum(int(room.get("facts", {}).get("chars") or 0) for room in rooms)
    story = len(mv.render_story(snapshot, floor.kept_level(snapshot, plan.projection(mode).memory_facts)))
    total = chars + story
    share = f"{100 * chars / total:.1f}%" if total else "0%"
    drafts = len(getattr(snapshot, "candidates", ()))
    return f"captured unsealed rooms {len(rooms)}, {chars:,} characters ({share} of story + open rooms); drafts awaiting a decision {drafts}"


def window_projection(record):
    """Pure sourced projection shared by Main, Settings and terminal failures."""
    from dataclasses import asdict

    limit = record.limiting
    observed = limit.observed_at if limit else ""
    source = limit.source if limit else "unknown"
    labels = {"user_setting": "set in Settings", "owner_ack": "owner confirmation",
              "provider_metadata": "server metadata", "local_health": "local serving window"}
    label = labels.get(source, source)
    dated = f"{label} {observed.replace('T', ' ')[:16]}".strip()
    if limit:
        text = f"window {limit.tokens:,} {limit.kind}: {dated}; usable input {record.usable:,}"
    else:
        text = "window unknown"
        if record.accepted:
            text += f"; accepted input ≥ {record.accepted:,} ({record.accepted_at.replace('T', ' ')[:16]})"
    text += f"; reply reserve {record.reserve:,}"
    contradiction = asdict(record.contradicted_by) if record.contradicted_by else None
    if contradiction:
        label = labels.get(contradiction["source"], contradiction["source"])
        text += (f"; {label} says {contradiction['tokens']:,} {contradiction['kind']}"
                 f" ({contradiction['observed_at'].replace('T', ' ')[:16]})")
    if record.retired_ack:
        stamp = str(record.retired_ack.get("ts") or "date unknown").replace("T", " ")[:16]
        text += f"; old confirmation {record.retired_ack['window_tokens']:,}, {stamp} — not used"
    return dict(tokens=limit.tokens if limit else None, kind=limit.kind if limit else "unknown",
                reserve=record.reserve, source=source, observed_at=observed, contradiction=contradiction,
                retired_ack=record.retired_ack, usable=record.usable, text=text)


def window_fact(plan, reserve):
    from ouroboros.context_window import resolve_window

    if plan is None:
        return ""
    record = resolve_window(plan, int(reserve or 0), bound=getattr(plan, "window_witness_bound", True))
    return window_projection(record)["text"]
