"""Retryable Project/Main terminal publication, with no cognition or timer.

The first root terminal transition records provenance; readiness precedes all
effects and the Project receipt retains Main disposition after readiness retires.
A separate short-lived projection
lock serializes callbacks, never holding the result lock across reentrant IO.
The chat row's token heals append/receipt-write crashes. Main uses the existing
bounded outbox; its external send/register crash gap remains at-least-once.
"""
from __future__ import annotations

import json
import logging
import pathlib
import uuid
from contextlib import contextmanager
from typing import Any

from ouroboros.platform_layer import acquire_exclusive_file_lock, release_exclusive_file_lock
from ouroboros.task_results import (
    is_reconciled_presence_placeholder, load_task_result, resolve_task_lineage, task_result_path, write_task_result,
)
from ouroboros.utils import jsonl_chain_handles, utc_now_iso

log = logging.getLogger(__name__)
SETTLEMENT_NONE, SETTLEMENT_DEFERRED, SETTLEMENT_SETTLED = "none", "deferred", "settled"


def _settled(row: dict) -> bool:
    """Settled for publication: a host-reconciled presence placeholder is not a result (the event
    re-runs), so it owes no terminal projection even when an earlier release already recorded readiness."""
    from ouroboros.task_status import SETTLED_STATUSES

    return row.get("status") in SETTLED_STATUSES and not is_reconciled_presence_placeholder(row)


def _lineage(tid: str, row: dict) -> dict:
    return resolve_task_lineage(tid, metadata=row.get("metadata"), **{
        key: row.get(key) for key in (
            "root_task_id", "parent_task_id", "delegation_role", "original_task_id", "timeout_retry_from",
        )
    })


def _attempt(row: dict) -> dict:
    # An obligation token distinguishes two publications even when old records
    # lack attempt metadata. These facts additionally fence same-id retries.
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return {**{key: row.get(key) for key in ("task_attempt", "_attempt", "started_at")},
            "metadata_attempt": metadata.get("attempt"), "metadata_task_attempt": metadata.get("task_attempt")}


def _witness(row: dict) -> dict:
    return {"attempt": _attempt(row), "status": row.get("status"),
            "checkpoint": row.get("root_phase_checkpoint"),
            "artifact_status": row.get("artifact_status"),
            "artifact_bundle": row.get("artifact_bundle")}


def _files_ready(root: Any, tid: str, row: dict) -> bool:
    from ouroboros.headless import terminal_task_files_ready

    return terminal_task_files_ready(pathlib.Path(root), {**row, "id": tid}, row)


def _open(row: dict) -> bool:
    checkpoint = row.get("root_phase_checkpoint") or {}
    return checkpoint.get("post_task_synthesis") in {"pending_once", "running"}


@contextmanager
def _publication_lock(root: Any, tid: str):
    path = task_result_path(root, tid, create=False).with_suffix(".projection.lock")
    fd = acquire_exclusive_file_lock(path, timeout_sec=0.01, poll_sec=0.005)
    try:
        yield fd is not None
    finally:
        if fd is not None:
            release_exclusive_file_lock(path, fd)


def _prepare(root: Any, tid: str, task: dict, event: dict) -> dict:
    """Durably record readiness from canonical terminal authority, before IO."""
    def prepare(current: dict, _patch: dict):
        if not _settled(current):
            return None
        effective = {**task, **current}
        if not _lineage(tid, effective)["is_root_task"]:
            return None
        ready = current.get("canonical_terminal_projection_ready")
        if (not isinstance(ready, dict)
                and current.get("canonical_terminal_projection_origin") != "terminal_transition"):
            # Terminal status alone is historical, not proof of new debt.
            # Existing readiness remains eligible across protocol upgrades.
            return None
        marker = current.get("canonical_terminal_projection")
        if isinstance(ready, dict) and ready.get("attempt") == _attempt(current):
            return None
        if isinstance(marker, dict) and not isinstance(ready, dict):
            # Pre-protocol Project receipts belonged to the legacy Main sender.
            # Do not replay those historical rows after delivered IDs age out.
            if "attempt" not in marker or marker["attempt"] == _attempt(current):
                return None
        chat_id = effective.get("chat_id")
        if chat_id is None:
            chat_id = event.get("chat_id", 0)
        return {"status": current["status"], "canonical_terminal_projection_ready": {
            "summary_id": f"task-terminal:{tid}", "token": uuid.uuid4().hex,
            "attempt": _attempt(current), "task_done_ts": str(event.get("ts") or current.get("ts") or utc_now_iso()),
            "chat_id": int(chat_id or 0),
        }}

    # Absence is not a terminal authority, and must never create a completed row.
    stored = load_task_result(root, tid, strict=True)
    if not stored or not _settled(stored):
        return stored or {}
    return write_task_result(root, tid, stored["status"], strict_existing_dict=True,
                             _field_projector=prepare)


def _project_row(tid: str, row: dict, event: dict, ready: dict) -> dict:
    from ouroboros import project_dialogue as dialogue
    from ouroboros.project_facts import resolve_project_id

    lineage = _lineage(tid, row)
    is_root = lineage["is_root_task"]
    role = str(row.get("role") or ("root" if is_root else "child"))
    parent = str(lineage["parent_task_id"] or "")
    phase = dialogue.outcome_phase(row, event)
    outcome = dialogue.OUTCOME_PHASE_HEADLINE[phase]
    chat_id = int(ready.get("chat_id", row.get("chat_id", event.get("chat_id", 0))) or 0)
    text = (f"{outcome}. Root task {tid}." if is_root
            else f"{outcome}. {role} (child {tid} of {parent or 'unknown'}).")
    verdict = dialogue._completion_verdict(row, event)
    excerpt = dialogue._completion_excerpt(row, chat_id=chat_id, salvage_only=True)
    text += "".join(f" {part}" for part in (verdict, excerpt) if part)
    from ouroboros.dialogue_provenance import presence_provenance_fields

    result = {
        "ts": str(ready.get("task_done_ts") or event.get("ts") or row.get("ts") or utc_now_iso()),
        "direction": "system", "type": "task_summary",
        **presence_provenance_fields(row),  # a presence room labels its terminal row like every other row
        "summary_id": f"task-terminal:{tid}",
        "summary_kind": "terminal_root_projection" if is_root else "terminal_result_projection",
        "task_id": tid, "parent_task_id": parent, "root_task_id": lineage["root_task_id"],
        "project_id": resolve_project_id(row), "chat_id": chat_id,
        "delegation_role": str(row.get("delegation_role") or ""), "role": role,
        "status": row["status"], "outcome": outcome, "outcome_phase": phase, "outcome_final": True,
        "outcome_authority": "canonical_task_result_after_finalization",
        "outcome_axes": row.get("outcome_axes") or {}, "reason_code": str(row.get("reason_code") or ""),
        "result_ref": {"kind": "task_result", "task_id": tid, "reader": "get_task_result"}, "text": text,
        **({"reason_detail": verdict} if verdict else {}),
        **({"terminal_projection_token": ready["token"]} if ready.get("token") else {}),
    }
    if isinstance(row.get("model_execution"), dict):
        result["model_execution"] = dict(row["model_execution"])
    return result


def _already_in_chat(root: Any, row: dict) -> bool:
    path = pathlib.Path(root) / "logs" / "chat.jsonl"
    # Pin the live inode before enumerating archives: rotation between append
    # and receipt persistence must not turn an existing row into an absence.
    with jsonl_chain_handles(path, strict=True) as handles:
        for segment, stream in handles:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    log.warning("Skipping malformed terminal projection history row at %s:%s", segment, line_number)
                    continue
                if not isinstance(entry, dict):
                    log.warning("Skipping non-object terminal projection history row at %s:%s", segment, line_number)
                    continue
                if (entry.get("summary_id") == row["summary_id"]
                        and entry.get("terminal_projection_token") == row.get("terminal_projection_token")):
                    return True
    return False


def _append_project(root: Any, tid: str, task: dict, event: dict) -> bool:
    from ouroboros import project_dialogue as dialogue

    stored = _prepare(root, tid, task, event)
    if not stored or not _settled(stored):
        return False
    effective = {**task, **stored}
    is_root = _lineage(tid, effective)["is_root_task"]
    ready = stored.get("canonical_terminal_projection_ready") or {}
    marker = stored.get("canonical_terminal_projection")
    if isinstance(marker, dict) and (not ready or marker.get("token") == ready.get("token")):
        return False
    if is_root and (not ready or _open(stored) or not _files_ready(root, tid, effective)):
        return False
    row = _project_row(tid, effective, {key: event[key] for key in ("ts", "chat_id") if key in event}, ready)
    appended = False
    if not is_root or not _already_in_chat(root, row):
        appended = dialogue.append_canonical_task_summary(root, row)
        if not appended:
            return False

    def receipt(current: dict, _patch: dict):
        if (_witness(current) != _witness(stored)
                or current.get("canonical_terminal_projection_ready") != stored.get("canonical_terminal_projection_ready")):
            return None
        return {"status": current["status"], "canonical_terminal_projection": {
            "summary_id": row["summary_id"], "summary_kind": row["summary_kind"],
            "written_at": row["ts"], "chat_id": row["chat_id"],
            "attempt": _attempt(stored), **({"token": ready["token"]} if ready.get("token") else {}),
        }}

    write_task_result(root, tid, stored["status"], strict_existing_dict=True, _field_projector=receipt)
    return appended


def append_terminal_projection(root: Any, tid: str, task: dict, event: dict, *, result: dict | None = None) -> bool:
    with _publication_lock(root, tid) as acquired:
        if not acquired:
            return False
        # Compatibility callers may hand over the first terminal observation.
        # Persist it before readiness/effects; never overwrite existing authority.
        if result and _settled(result) and load_task_result(root, tid, strict=True) is None:
            fields = {**(task or {}), **result}
            status = fields.pop("status")
            fields.pop("task_id", None)
            write_task_result(root, tid, status, create_only=True, strict_existing_dict=True, **fields)
        return _append_project(root, tid, task or {}, event or {})


def clear_terminal_projection_obligation(root: Any, tid: str, expected: dict, disposition: str) -> bool:
    """CAS the complete readiness token and canonical attempt/checkpoint witness."""
    if disposition not in {"owed", "ineligible"}:
        return False
    cleared = False

    def retire(current: dict, _patch: dict):
        nonlocal cleared
        ready = expected.get("canonical_terminal_projection_ready")
        marker = current.get("canonical_terminal_projection")
        if (not isinstance(ready, dict) or not isinstance(marker, dict)
                or current.get("canonical_terminal_projection_ready") != ready
                or marker != expected.get("canonical_terminal_projection")
                or _witness(current) != _witness(expected) or _open(current)
                or not _files_ready(root, tid, current)
                or marker.get("token") != ready.get("token")):
            return None
        cleared = True
        return {"status": current["status"], "canonical_terminal_projection_ready": None,
                "canonical_terminal_projection": {**marker, "main_disposition": disposition}}

    write_task_result(root, tid, str(expected.get("status") or "completed"),
                      strict_existing_dict=True, _field_projector=retire)
    return cleared


def settle_terminal_projection(drive_root: Any, task_id: str, *, task: dict | None = None,
                               event: dict | None = None) -> str:
    from ouroboros import project_dialogue as dialogue

    tid = str(task_id or "").strip()
    if not tid:
        return SETTLEMENT_NONE
    try:
        with _publication_lock(drive_root, tid) as acquired:
            if not acquired:
                return SETTLEMENT_DEFERRED
            stored = _prepare(drive_root, tid, task or {}, event or {})
            if (not isinstance(stored.get("canonical_terminal_projection_ready"), dict)
                    or is_reconciled_presence_placeholder(stored)):  # readiness an earlier release recorded
                return SETTLEMENT_NONE
            if (_open(stored) or not _settled(stored)
                    or not _files_ready(drive_root, tid, {**(task or {}), **stored})):
                return SETTLEMENT_DEFERRED
            _append_project(drive_root, tid, task or {}, event or {})
            # IO may have advanced canonical state. Render Main from the NEW
            # result, never the task_done snapshot passed to this continuation.
            stored = load_task_result(drive_root, tid, strict=True) or {}
            ready = stored.get("canonical_terminal_projection_ready")
            marker = stored.get("canonical_terminal_projection")
            if (not isinstance(ready, dict) or not isinstance(marker, dict)
                    or marker.get("token") != ready.get("token") or _open(stored)
                    or not _settled(stored)
                    or not _files_ready(drive_root, tid, {**(task or {}), **stored})):
                return SETTLEMENT_DEFERRED
            effective = {**(task or {}), **stored, "id": tid}
            done = {**(event or {}), **stored, "ts": ready["task_done_ts"], "chat_id": ready["chat_id"]}
            retired = False

            def retire_owed() -> bool:
                nonlocal retired
                retired = clear_terminal_projection_obligation(drive_root, tid, stored, "owed")
                return retired

            disposition, _queued = dialogue.project_completion_delivery_outcome(
                drive_root, done, tid, effective, stored, done, _on_owed=retire_owed)
            if retired or clear_terminal_projection_obligation(drive_root, tid, stored, disposition):
                return SETTLEMENT_SETTLED
            return SETTLEMENT_DEFERRED
    except Exception:
        log.warning("Terminal projection deferred for %s", tid, exc_info=True)
        return SETTLEMENT_DEFERRED


def reconcile_terminal_projections(drive_root: Any) -> int:
    """Discover the terminal-write/readiness-write crash gap on the existing pass.

    Never re-enter synthesis or infer debt from historical terminal status. Read
    each authority strictly: one bad sibling must not stall others or quarantine
    unknown bytes via a tolerant scan.
    """
    from ouroboros.task_results import task_results_dir

    settled = 0
    for path in sorted(task_results_dir(drive_root, create=False).glob("*.json")):
        try:
            row = load_task_result(drive_root, path.stem, strict=True)
            if not row or not _settled(row):
                continue
            ready = row.get("canonical_terminal_projection_ready")
            if (not isinstance(ready, dict)
                    and row.get("canonical_terminal_projection_origin") != "terminal_transition"):
                continue
            marker = row.get("canonical_terminal_projection")
            if isinstance(marker, dict) and not isinstance(ready, dict):
                if "attempt" not in marker or marker["attempt"] == _attempt(row):
                    continue
            tid = row["task_id"]
            if _lineage(tid, row)["is_root_task"] and not _open(row):
                settled += settle_terminal_projection(drive_root, tid) == SETTLEMENT_SETTLED
        except Exception:
            log.warning("Terminal projection reconciliation deferred for %s", path, exc_info=True)
    return settled
