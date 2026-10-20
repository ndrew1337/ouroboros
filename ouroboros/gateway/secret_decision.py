"""``POST /api/decisions/secret`` — the protected form of an integration-secret request.

The browser's ``saveSecretToken`` sends ``{request_id, decision_id, values}`` here and nowhere
else; ``action: "cancel"`` closes the request with nothing saved. ``decision_id`` is the
request's ordinary question identity ``quiz:{task_id}:{quiz_id}``, so status is the existing
exact-question read (``GET /api/tasks/{task_id}``) and recovery is the existing question debt.

A value is held in this request's memory only: validated against the request's own declared
fields, written by the owner Settings writer (``_owner_update_settings`` with
``fields_on_saved_document``) under the settings document lock, and dropped. Every response,
refusal, log line, audit row, history row and mailbox frame carries field ids, Settings key
names and lifecycle facts only. ``saved`` is a field on both sides of the commit boundary: a
refusal says ``saved: false``; a write that landed but could not be verified or recorded says
``saved: true`` with ``secret_save_unverified``/``secret_completion_unrecorded``; an attempt of
unknown outcome says ``saved: null`` (``secret_save_unconfirmed``) and is never repeated under
its own request — the owner reads status and decides to save again with a new one. ``POST /api/decisions`` refuses an ordinary
answer to such a question (``secret_request_form_only``) and routes its closure here.
"""

from __future__ import annotations

import logging
import pathlib
from types import SimpleNamespace
from typing import Any, Dict, Tuple

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros import secret_requests as sr
from ouroboros.gateway._helpers import request_drive_root, request_json_or, run_sync_to_completion

log = logging.getLogger(__name__)

_REQUEST_ID_MAX = 128
_FIELDS = {"request_id", "decision_id", "action", "values"}
_NO_STORE = {"Cache-Control": "no-store"}


def _refusal(status: int, code: str, message: str, decision_id: str, block: Any = None,
             *, saved: Any = False) -> Tuple[int, Dict[str, Any]]:
    payload: Dict[str, Any] = {"ok": False, "saved": saved, "error": message, "reason_code": code,
                               "decision_id": decision_id}
    if isinstance(block, dict) and block:
        payload.update(_lifecycle(block))
    return status, payload


def _lifecycle(block: Dict[str, Any]) -> Dict[str, Any]:
    """The request's current facts, so a losing tab settles on the winner instead of retrying."""
    from ouroboros.owner_quiz import CLOSURE_FIELDS, question_delivery_projection

    view: Dict[str, Any] = {"state": str(block.get("state") or "")}
    outcome = block.get("secret_outcome") if isinstance(block.get("secret_outcome"), dict) else {}
    if outcome.get("event") in (sr.EVENT_FILLED, sr.EVENT_CANCELLED):
        event = sr.public_event(block)
        view[event["event"]] = {"pluginName": event["pluginName"], "fields": event["fields"]}
        view["secret_outcome"] = dict(outcome)
        view.update({key: outcome[key] for key in ("settings_keys", "grant_missing", "grant_unknown",
                                                   "grant_unsupported", "interrupted_save") if key in outcome})
    claim = block.get("secret_save") if isinstance(block.get("secret_save"), dict) else {}
    if claim.get("state") == sr.SAVE_WRITING:
        view["save_unconfirmed"] = True  # an attempt nobody here holds: its outcome is unknown
    view.update({key: block[key] for key in CLOSURE_FIELDS if key in block})
    if delivery := question_delivery_projection(block):
        view["question_delivery"] = delivery
    return view


def _receipt(decision_id: str, block: Dict[str, Any], *, duplicate: bool) -> Dict[str, Any]:
    """``saved`` is true for a recorded fill, null when an interrupted earlier attempt's write is
    unknown (a cancel after it saved nothing itself, yet cannot say nothing is stored), else false."""
    outcome = block.get("secret_outcome") if isinstance(block.get("secret_outcome"), dict) else {}
    saved = True if outcome.get("event") == sr.EVENT_FILLED else None if outcome.get("interrupted_save") else False
    return {"ok": True, "decision_id": decision_id, "duplicate": duplicate, "saved": saved, **_lifecycle(block)}


def _parse(body: Any) -> Tuple[str, str, str, str, str]:
    if not isinstance(body, dict):
        raise sr.SecretRequestError("request_invalid", "request body must be a JSON object.")
    if set(body) - _FIELDS:
        raise sr.SecretRequestError("unexpected_fields", "a secret decision accepts only "
                                    "{request_id, decision_id, action?, values?}.")
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > _REQUEST_ID_MAX:
        raise sr.SecretRequestError("request_id_required", "request_id is required "
                                    "(a stable client-generated id, reused only for the same values).")
    decision_id = str(body.get("decision_id") or "").strip()
    parts = decision_id.split(":", 2)
    if len(parts) != 3 or parts[0] != "quiz" or not parts[1] or not parts[2]:
        raise sr.SecretRequestError("malformed_decision_id", "decision_id must be quiz:{task_id}:{quiz_id}.")
    from ouroboros.task_results import validate_task_id

    try:
        task_id = validate_task_id(parts[1])
    except ValueError:
        raise sr.SecretRequestError("malformed_decision_id", "decision_id names an invalid task id.") from None
    action = body.get("action", "save")
    if action not in ("save", "cancel"):
        raise sr.SecretRequestError("action_invalid", "action must be save or cancel.")
    if action == "cancel" and "values" in body:
        raise sr.SecretRequestError("cancel_payload_invalid", "a cancel carries no values.")
    return request_id.strip(), decision_id, task_id, parts[2], action


def _send_state(block: Dict[str, Any], task_id: str) -> None:
    """Best-effort live update; the canonical block is the truth and history replays it."""
    try:
        from supervisor.message_bus import get_bridge

        view = _lifecycle(block)
        get_bridge().send_quiz_state(
            str(block.get("quiz_id") or ""), task_id, view.pop("state"),
            secret_outcome=block.get("secret_outcome"), question_delivery=view.get("question_delivery"),
            closed_reason=view.get("closed_reason"), closed_at=view.get("closed_at"),
            closed_actor=view.get("closed_actor"), closed_request_id=view.get("closed_request_id"),
            chat_id=int(block.get("publication_chat_id") or 0))
    except Exception:
        log.debug("secret request state broadcast failed for %s", block.get("quiz_id"), exc_info=True)


def deliver_secret_outcome(drive_root: pathlib.Path, task_id: str, quiz_id: str, *,
                           recovery: bool = False) -> Dict[str, Any]:
    """Deliver a recorded ``secret_filled`` like an answer: history row, then the asker's
    mailbox (which ends its wait). With no reader the history row is the session's record;
    the outcome is never forwarded as an owner turn."""
    from ouroboros.gateway.task_decision import _late_answer_destination, _live_root_task, _record_quiz_answer_history
    from ouroboros.owner_quiz import (
        claim_quiz_delivery, question_delivery_settled, quiz_states, record_quiz_delivery_failure,
    )
    from ouroboros.question_answers import deliver_answer

    block = quiz_states(drive_root, task_id).get(quiz_id) or {}
    if block.get("state") != "answered" or question_delivery_settled(block) or sr.secret_request_of(block) is None:
        return block
    claimed = claim_quiz_delivery(drive_root, task_id, quiz_id, force=not recovery)
    if not claimed:
        return block
    destination, reason = _late_answer_destination(drive_root, task_id, claimed)
    if destination is not None:
        try:
            _record_quiz_answer_history(drive_root, task_id, None, claimed, duplicate=False)
        except Exception as exc:
            log.warning("Secret request history row remains owed for %s: %s", quiz_id, type(exc).__name__)
            record_quiz_delivery_failure(drive_root, task_id, quiz_id, "quiz_history_write_failed")
            return quiz_states(drive_root, task_id).get(quiz_id) or claimed

    def history_only(root: Any, asker: str, qid: str, _block: Any, source: str = "web") -> Tuple[bool, str]:
        # Pause/hold is not the end of the asker: retain the existing answer debt
        # until recovery can mail this host outcome to the same reading task.
        _task, live_reason = _live_root_task(asker)
        if live_reason == "task_not_reading":
            return False, "awaiting_recipient_read"
        written = ((quiz_states(root, asker).get(qid) or {}).get("delivery") or {}).get("history_at")
        return (True, "") if written else (False, reason or "publication_address_unknown")

    _forwarded, why = deliver_answer(drive_root, task_id, quiz_id, claimed, _live_root_task, history_only)
    if reason or why:
        record_quiz_delivery_failure(drive_root, task_id, quiz_id, reason or why)
    return quiz_states(drive_root, task_id).get(quiz_id) or claimed


def _save_locked(drive_root: pathlib.Path, decision_id: str, task_id: str, quiz_id: str, request_id: str,
                 request: Dict[str, Any], changes: Dict[str, str]) -> Tuple[int, Dict[str, Any], bool]:
    """Claim -> file-locked current mapping/write -> completion, under the document lock."""
    from ouroboros.gateway.owner_settings import (
        CommitBoundary, SettingsLockUnavailable, SettingsPreconditionFailed, _owner_settings_document,
        _owner_update_settings, fields_on_saved_document,
    )

    claim = sr.claim_save(drive_root, task_id, quiz_id, request_id)
    if not claim.get("ok"):
        code = str(claim.get("error") or "secret_request_refused")
        messages = {
            "secret_request_not_found": "No such secret request.",
            "not_a_secret_request": "This question is not a secret request.",
            "secret_request_inactive": "The task that asked has finished; nothing was saved. Ask again, or save the key in Settings.",
            "secret_request_settled": "This secret request was already completed or cancelled; nothing was saved.",
            "secret_save_unconfirmed": "This save attempt was never confirmed and its result is unknown, so it "
                                       "is not repeated. Check status; saving again starts a new request.",
        }
        return (*_refusal(404 if code == "secret_request_not_found" else 409, code,
                          messages.get(code, "The secret request was refused; nothing was saved."),
                          decision_id, claim.get("block"),
                          saved=None if code == "secret_save_unconfirmed" else False), False)
    if claim.get("duplicate"):
        return 200, _receipt(decision_id, claim["block"], duplicate=True), False
    previous = dict(claim.get("previous_claim") or {})
    boundary = CommitBoundary()
    try:
        # The file lock may wait for another process. Check the skill on the write side
        # of that wait, not against the mapping seen before entering the Settings writer.
        _owner_update_settings(fields_on_saved_document(dict(changes)), boundary=boundary,
                               precondition=lambda: sr.stale_reason(drive_root, request))
    except Exception as exc:
        if boundary.committed:  # unreachable today: the write is the writer's last step
            log.warning("Secret saved; a later settings step failed: %s", type(exc).__name__)
        else:
            sr.release_save(drive_root, task_id, quiz_id, request_id, previous)
            if isinstance(exc, SettingsPreconditionFailed):
                return (*_refusal(409, "secret_request_stale", "The skill's secret mapping changed since the "
                                  "request; nothing was saved. Ask again.", decision_id), False)
            log.warning("Secret save refused before commit: %s", type(exc).__name__)
            code = "settings_locked" if isinstance(exc, SettingsLockUnavailable) else "secret_save_failed"
            return (*_refusal(503, code, f"Settings could not be written ({type(exc).__name__}); nothing was "
                              "saved. Try again.", decision_id), False)
    try:
        document = _owner_settings_document()
        retained = all(document.get(key) == value for key, value in changes.items())
    except Exception as exc:
        # The commit is known; that Settings reads the value back is not. The claim stays
        # ``writing`` (unknown) and nothing claims ``secret_filled`` on an unverified write.
        log.warning("Secret written; its read-back failed: %s", type(exc).__name__)
        return (*_refusal(500, "secret_save_unverified", "The secret was written to Settings, but reading it "
                          "back failed, so it is not reported filled and the task was not told. Check status "
                          "before saving again.", decision_id, saved=True), False)
    if not retained:
        sr.release_save(drive_root, task_id, quiz_id, request_id, previous)
        return (*_refusal(500, "secret_not_retained", "Settings did not keep the value as entered, so it is "
                          "not reported saved and the task was not told. Check the value and try again.",
                          decision_id, saved=False), False)
    try:
        final = sr.finalize_save(drive_root, task_id, quiz_id, request_id, sr.grant_facts(drive_root, request))
    except Exception as exc:
        log.warning("Secret saved; its completion could not be recorded: %s", type(exc).__name__)
        final = {}
    if not final.get("ok"):
        return (*_refusal(500, "secret_completion_unrecorded", "The secret was saved to Settings, but the request "
                          "could not be marked complete and the task was not told. Check status before trying again.",
                          decision_id, saved=True), False)
    return 200, _receipt(decision_id, final["block"], duplicate=False), True


def _expire_if_asker_finished(drive_root: pathlib.Path, task_id: str) -> None:
    """An ``open`` block is a projection the task-done seam may not have reached yet (a crash
    or post-work window). Heal it from the live queue exactly as an ordinary answer does, so a
    finished asker's request reads ``expired_terminal`` and nothing is written for it."""
    from ouroboros.gateway.task_decision import _live_root_task, _send_quiz_state
    from ouroboros.owner_quiz import reconcile_terminal

    try:
        task, refusal = _live_root_task(task_id)
        if task is None and refusal not in ("task_not_reading", "not_a_root_task"):
            for qid in reconcile_terminal(drive_root, task_id):
                _send_quiz_state(qid, task_id, "expired_terminal")
    except Exception as exc:  # the block's own state still decides, as before this read
        log.warning("Secret request could not read its asking task: %s", type(exc).__name__)


def save_secret(drive_root: pathlib.Path, body: Any) -> Tuple[int, Dict[str, Any]]:
    from ouroboros.gateway.owner_settings import SettingsDocumentBusy, settings_document_mutation
    from ouroboros.owner_quiz import quiz_states

    try:
        request_id, decision_id, task_id, quiz_id, action = _parse(body)
    except sr.SecretRequestError as exc:
        return _refusal(400, exc.code, str(exc), "")  # nothing the client sent is echoed before it parses
    if action == "cancel":
        return cancel_secret(drive_root, decision_id, task_id, quiz_id, request_id)
    block = quiz_states(drive_root, task_id).get(quiz_id)
    request = sr.secret_request_of(block)
    if not block or request is None:
        return _refusal(404 if not block else 409, "secret_request_not_found" if not block else "not_a_secret_request",
                        "No such secret request." if not block else "This question is not a secret request.", decision_id)
    try:
        changes = sr.validate_values(request, body.get("values"))
    except sr.SecretRequestError as exc:
        return _refusal(400, exc.code, str(exc), decision_id)
    _expire_if_asker_finished(drive_root, task_id)
    try:
        with settings_document_mutation():
            status, payload, deliver = _save_locked(drive_root, decision_id, task_id, quiz_id, request_id, request, changes)
    except SettingsDocumentBusy as exc:
        return _refusal(503, "settings_busy", str(exc), decision_id)
    finally:
        changes.clear()
    if deliver:
        try:
            block = deliver_secret_outcome(drive_root, task_id, quiz_id)
        except Exception as exc:
            from ouroboros.owner_quiz import record_quiz_delivery_failure

            log.warning("Secret request delivery remains owed for %s: %s", quiz_id, type(exc).__name__)
            record_quiz_delivery_failure(drive_root, task_id, quiz_id, "quiz_delivery_failed")
            block = quiz_states(drive_root, task_id).get(quiz_id) or {}
        payload = _receipt(decision_id, block, duplicate=False) if block else payload
        _send_state(block, task_id)
    return status, payload


def cancel_secret(drive_root: pathlib.Path, decision_id: str, task_id: str, quiz_id: str,
                  request_id: str) -> Tuple[int, Dict[str, Any]]:
    """Owner cancel: a closure with typed ``secret_cancelled`` facts; first winner against a save."""
    from ouroboros.gateway.owner_settings import SettingsDocumentBusy, settings_document_mutation
    from ouroboros.owner_quiz import quiz_states, record_closed

    block = quiz_states(drive_root, task_id).get(quiz_id)
    if not block or sr.secret_request_of(block) is None:
        return _refusal(404 if not block else 409, "secret_request_not_found" if not block else "not_a_secret_request",
                        "No such secret request." if not block else "This question is not a secret request.", decision_id)
    try:
        with settings_document_mutation():
            current = quiz_states(drive_root, task_id).get(quiz_id) or block
            facts = sr.cancel_outcome(current)
            reason = sr.CANCEL_REASON_INTERRUPTED if facts.get("interrupted_save") else sr.CANCEL_REASON
            outcome = record_closed(drive_root, task_id, quiz_id=quiz_id, reason=reason, actor="owner",
                                    request_id=request_id, secret_outcome=facts)
    except SettingsDocumentBusy as exc:
        return _refusal(503, "settings_busy", str(exc), decision_id)
    block = outcome.get("block") or {}
    if not outcome.get("ok"):
        code = str(outcome.get("error") or "secret_cancel_refused")
        return _refusal(404 if code == "quiz_not_found" else 409, code,
                        "This secret request was already completed or closed." if code != "quiz_not_found"
                        else "No such secret request.", decision_id, block)
    if not outcome.get("duplicate"):
        from supervisor.events_chat_delivery import _handle_send_quiz_state
        from supervisor.message_bus import get_bridge

        try:
            _handle_send_quiz_state({"task_id": task_id, "quiz_id": quiz_id, "owner_retry": True},
                                    SimpleNamespace(DRIVE_ROOT=drive_root, bridge=get_bridge()))
        except Exception as exc:
            log.warning("Secret request cancellation delivery remains owed for %s: %s", quiz_id, type(exc).__name__)
    block = quiz_states(drive_root, task_id).get(quiz_id) or block
    return 200, _receipt(decision_id, block, duplicate=bool(outcome.get("duplicate")))


def is_secret_question(drive_root: pathlib.Path, task_id: str, quiz_id: str) -> bool:
    from ouroboros.owner_quiz import quiz_states

    return sr.secret_request_of(quiz_states(drive_root, task_id).get(quiz_id)) is not None


async def api_secret_decision(request: Request) -> JSONResponse:
    """POST /api/decisions/secret — ``saveSecretToken`` and its cancel."""
    from ouroboros.gateway.owner_settings import owner_audit

    body = await request_json_or(request, None)
    status, payload = await run_sync_to_completion(save_secret, request_drive_root(request), body)
    cancel = isinstance(body, dict) and body.get("action") == "cancel"
    body = None  # the values leave this frame with the request; the audit names only host facts
    event = payload.get("secret_cancelled" if cancel else "secret_filled") or {}
    owner_audit(request, "secret_request_cancel" if cancel else "secret_request_save", {
        "decision_id": str(payload.get("decision_id") or ""), "plugin": str(event.get("pluginName") or ""),
        "fields": list(event.get("fields") or []),
        "outcome": "ok" if payload.get("ok") else str(payload.get("reason_code") or "refused"),
        "saved": payload.get("saved"),
    })
    return JSONResponse(payload, status_code=status, headers=_NO_STORE)


__all__ = ["api_secret_decision", "cancel_secret", "deliver_secret_outcome", "is_secret_question", "save_secret"]
