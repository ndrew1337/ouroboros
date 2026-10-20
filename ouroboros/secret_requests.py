"""Integration-secret requests: ``showSecretHitl`` asks, the owner's protected form saves.

One request is one owner question (``owner_quiz``) whose block carries typed, metadata-only
facts beside the ordinary lifecycle, so publication, waiting, history, recovery and the Main
mirror stay the existing question machinery:

    "secret_request": {"plugin_name", "fields": [{"id", "setting_key"}], "content_hash"},
    "secret_save"?:   {"request_id", "state": writing|saved, "started_at", "saved_at"?},
    "secret_outcome"?: {"event": secret_filled|secret_cancelled, "pluginName", "fields",
                        "at", "settings_keys", "grant_missing"?, "grant_unknown"?,
                        "interrupted_save"?}

A value travels exactly once: browser password control -> ``POST /api/decisions/secret`` ->
the existing Settings writer (``settings.json``). It never enters a tool argument or result, a
quiz comment, a mailbox frame, a history row, an event, a log line, an error or a response;
the model learns ``secret_filled {pluginName, fields}`` and nothing else.

Which Settings key a field writes is the skill's reviewed declaration, never the request's:
the canonical ``SkillManifest.secret_fields`` maps public field ids to keys the manifest
already lists in ``env_from_settings`` (``secret_fields: {botToken: TELEGRAM_BOT_TOKEN}``).
Such a key may be an ordinary setting, so the host admits only an owner-defined custom
secret key: no plugin name can front a provider, network or core credential. The ask
binds the skill's content hash; a changed, removed or broken skill makes the pending request
stale and nothing is written. Saving grants nothing, enables nothing, reloads nothing and
contacts no provider: the outcome says what the skill's current grant covers.

First winner: every save and cancel of a request runs under the one in-process settings
document lock, and a save claims the block (``secret_save.state == writing``) before its
write, so a closure from any other path is refused while a write may be in flight. Under that
lock a ``writing`` claim not held by the caller is an interrupted attempt of an earlier
process: its result is unknown and never reported as saved. Recovery is status first: the
same request is never repeated over it (it may already have written), and only a NEW owner
request saves again, replacing whatever the interrupted attempt may have stored.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from ouroboros.utils import utc_now_iso

MAX_SECRET_VALUE_CHARS = 4096
EVENT_FILLED = "secret_filled"
EVENT_CANCELLED = "secret_cancelled"
SAVE_WRITING = "writing"
SAVE_SAVED = "saved"
CANCEL_REASON = "The owner cancelled the secret request; nothing was saved."
CANCEL_REASON_INTERRUPTED = ("The owner cancelled the secret request; an earlier save attempt was never "
                             "confirmed, so whether its value reached Settings is unknown.")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class SecretRequestError(ValueError):
    """A typed refusal whose message names fields and keys, never a value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def declared_secret_fields(manifest: Any) -> Dict[str, str]:
    """The manifest's ``secret_fields`` as ``{field_id: settings_key}`` (empty when absent).

    The parser already proved the shape and that each key is in ``env_from_settings``; this
    re-checks both (a hand-built manifest is not parsed) and adds the host's classification:
    every target must be an owner-defined custom Settings secret, so provider, network and
    core keys are refused even when listed.
    """
    from ouroboros.contracts.skill_manifest import SkillManifestError, _secret_fields

    raw = getattr(manifest, "secret_fields", None) or {}
    try:
        fields = _secret_fields(raw, list(getattr(manifest, "env_from_settings", None) or []))
    except SkillManifestError as exc:
        raise SecretRequestError("SECRET_FIELDS_INVALID", str(exc)) from None
    from ouroboros.config import SETTINGS_DEFAULTS
    from ouroboros.contracts.plugin_api import FORBIDDEN_SKILL_SETTINGS
    from ouroboros.secret_masking import MASKED_SECRET_SETTING_KEYS, is_custom_secret_setting_key

    for field_id, key in fields.items():
        if (key in FORBIDDEN_SKILL_SETTINGS or key in MASKED_SECRET_SETTING_KEYS
                or not is_custom_secret_setting_key(key, known_setting_keys=SETTINGS_DEFAULTS)):
            raise SecretRequestError("SECRET_FIELDS_INVALID", f"secret field {field_id} targets {key}, which is not "
                                     "a custom Settings secret; provider, network and core keys stay in Settings.")
    return fields


def _find_skill(drive_root: Any, plugin_name: str) -> Any:
    from ouroboros.config import get_skills_repo_path
    from ouroboros.skill_loader import find_skill

    return find_skill(drive_root, plugin_name, repo_path=get_skills_repo_path() or None)


def resolve_secret_request(drive_root: Any, plugin_name: Any, fields: Any) -> Dict[str, Any]:
    """The request's typed metadata for ``plugin_name``'s declared ``fields``, or a refusal."""
    from ouroboros.contracts.skill_manifest import MAX_SECRET_FIELDS

    name = str(plugin_name or "").strip()
    if not name:
        raise SecretRequestError("SECRET_PLUGIN_REQUIRED", "pluginName must name an installed skill.")
    skill = _find_skill(drive_root, name)
    if skill is None or getattr(skill, "identity_collision", False):
        raise SecretRequestError("SECRET_PLUGIN_UNKNOWN", f"no single installed skill is named {name!r}.")
    if skill.load_error:
        raise SecretRequestError("SECRET_PLUGIN_BROKEN", f"skill {skill.name} has a manifest error; fix it first.")
    declared = declared_secret_fields(skill.manifest)
    if not declared:
        raise SecretRequestError("SECRET_FIELDS_UNDECLARED", f"skill {skill.name} declares no secret_fields in its "
                                 "manifest, so the host knows no protected field to ask for.")
    if not isinstance(fields, list) or not fields or len(fields) > MAX_SECRET_FIELDS:
        raise SecretRequestError("SECRET_FIELDS_INVALID", f"fields must list 1 to {MAX_SECRET_FIELDS} declared field ids.")
    requested: List[str] = []
    for raw in fields:
        field_id = str(raw or "").strip()
        if field_id not in declared:
            raise SecretRequestError("SECRET_FIELD_UNDECLARED", f"{skill.name} declares the secret fields "
                                     f"{', '.join(sorted(declared))}; {field_id or '(empty)'} is not one of them.")
        if field_id in requested:
            raise SecretRequestError("SECRET_FIELDS_INVALID", f"field {field_id} is listed twice.")
        requested.append(field_id)
    return {"plugin_name": skill.name, "content_hash": str(skill.content_hash or ""),
            "fields": [{"id": field_id, "setting_key": declared[field_id]} for field_id in requested]}


def stale_reason(drive_root: Any, request: Dict[str, Any]) -> str:
    """Why the asked mapping no longer holds (``""`` while it does): the skill is gone or
    broken, its reviewed content changed, or a field now names another key."""
    try:
        current = resolve_secret_request(drive_root, request.get("plugin_name"),
                                         [row.get("id") for row in request.get("fields") or []])
    except SecretRequestError as exc:
        return exc.code.lower()
    except Exception as exc:  # an unreadable skill tree is not a current mapping
        return f"skill_unreadable:{type(exc).__name__}"
    if current["content_hash"] != str(request.get("content_hash") or ""):
        return "skill_changed"
    if current["fields"] != request.get("fields"):
        return "mapping_changed"
    return ""


def secret_request_of(block: Any) -> Optional[Dict[str, Any]]:
    request = block.get("secret_request") if isinstance(block, dict) else None
    return request if isinstance(request, dict) and request.get("plugin_name") else None


def field_ids(request: Dict[str, Any]) -> List[str]:
    return [str(row.get("id") or "") for row in request.get("fields") or []]


def settings_keys(request: Dict[str, Any]) -> List[str]:
    return [str(row.get("setting_key") or "") for row in request.get("fields") or []]


def public_event(block: Dict[str, Any]) -> Dict[str, Any]:
    """The screenshot's event: its name, ``pluginName`` and the field ids — no value."""
    outcome = block.get("secret_outcome") if isinstance(block.get("secret_outcome"), dict) else {}
    request = secret_request_of(block) or {}
    return {"event": str(outcome.get("event") or ""), "pluginName": str(request.get("plugin_name") or ""),
            "fields": field_ids(request)}


def question_text(request: Dict[str, Any]) -> str:
    """The card's host-authored question: what is asked, never how it is used beyond saving."""
    return (f"Secret required: {request['plugin_name']} — {', '.join(field_ids(request))}. "
            "Enter it in the protected form; it is saved to Settings without reaching the model.")


def validate_values(request: Dict[str, Any], values: Any) -> Dict[str, str]:
    """``{settings_key: value}`` for exactly the asked fields, or a refusal naming only fields.

    A value Settings would read back as a masked placeholder (and blank on load) is refused
    rather than reported saved; so is anything a password control cannot carry.
    """
    from ouroboros.secret_masking import looks_masked_settings_secret

    asked = {str(row["id"]): str(row["setting_key"]) for row in request.get("fields") or []}
    if not isinstance(values, dict) or set(values) != set(asked):
        raise SecretRequestError("secret_fields_mismatch", "values must carry exactly the requested fields: "
                                 f"{', '.join(asked)}.")
    changes: Dict[str, str] = {}
    for field_id, key in asked.items():
        value = values[field_id]
        if not isinstance(value, str) or not value.strip():
            raise SecretRequestError("secret_value_empty", f"{field_id} is empty.")
        if len(value) > MAX_SECRET_VALUE_CHARS:
            raise SecretRequestError("secret_value_too_long", f"{field_id} is longer than {MAX_SECRET_VALUE_CHARS} characters.")
        if _CONTROL_RE.search(value):
            raise SecretRequestError("secret_value_invalid", f"{field_id} contains a line break or control character.")
        if looks_masked_settings_secret(key, value):
            raise SecretRequestError("secret_value_placeholder", f"{field_id} has the shape of a masked placeholder, "
                                     "which Settings would not keep; enter the real value.")
        changes[key] = value
    return changes


def _projection():
    from ouroboros.owner_quiz import _KEEP, _delivery_debt, _mutate_projection

    return _KEEP, _delivery_debt, _mutate_projection


def claim_save(drive_root: Any, task_id: str, quiz_id: str, request_id: str) -> Dict[str, Any]:
    """Claim the block for one save (caller holds the settings document lock).

    ``duplicate`` replays a recorded success without writing again. A ``writing`` claim is
    an attempt whose outcome is unknown (the lock proves nobody here still holds it): the SAME
    request is refused (``secret_save_unconfirmed``) rather than repeated blind over a write
    that may have landed; a NEW request is the owner's explicit save-again and takes it over."""
    keep, _debt, mutate_projection = _projection()
    outcome: Dict[str, Any] = {}

    def mutate(quizzes: Dict[str, Dict[str, Any]]) -> Any:
        block = quizzes.get(str(quiz_id))
        if not block:
            outcome.update(error="secret_request_not_found")
            return keep
        if secret_request_of(block) is None:
            outcome.update(error="not_a_secret_request")
            return keep
        state = str(block.get("state") or "open")
        if (state == "answered" and (block.get("secret_outcome") or {}).get("event") == EVENT_FILLED
                and block.get("request_id") == request_id):
            outcome.update(ok=True, duplicate=True, block=dict(block))
            return keep
        if state != "open":
            outcome.update(error="secret_request_inactive" if state == "expired_terminal" else "secret_request_settled",
                           block=dict(block))
            return keep
        claim = dict(block.get("secret_save") or {})
        outcome["previous_claim"] = claim
        if claim.get("state") == SAVE_WRITING and claim.get("request_id") == request_id:
            outcome.update(error="secret_save_unconfirmed", block=dict(block))
            return keep
        block["secret_save"] = {"request_id": request_id, "state": SAVE_WRITING, "started_at": utc_now_iso(),
                                **({"interrupted_request_id": str(claim.get("request_id") or "")}
                                   if claim.get("state") == SAVE_WRITING else {})}
        outcome.update(ok=True, block=dict(block))
        return True

    mutate_projection(drive_root, task_id, mutate)
    return outcome


def release_save(drive_root: Any, task_id: str, quiz_id: str, request_id: str, previous: Dict[str, Any]) -> None:
    """Nothing was written by ``request_id``: restore what the block said before its claim."""
    keep, _debt, mutate_projection = _projection()

    def mutate(quizzes: Dict[str, Dict[str, Any]]) -> Any:
        block = quizzes.get(str(quiz_id))
        claim = (block or {}).get("secret_save") or {}
        if not block or claim.get("request_id") != request_id or claim.get("state") != SAVE_WRITING:
            return keep
        if previous:
            block["secret_save"] = dict(previous)
        else:
            block.pop("secret_save", None)
        return True

    mutate_projection(drive_root, task_id, mutate)


def finalize_save(drive_root: Any, task_id: str, quiz_id: str, request_id: str,
                  outcome_facts: Dict[str, Any]) -> Dict[str, Any]:
    """The value is in Settings: record the host's typed completion as the block's answer.

    Never a comment and never an option: ``secret_outcome`` is the answer, and the answer
    delivery debt is the same one an ordinary answer owes."""
    keep, delivery_debt, mutate_projection = _projection()
    result: Dict[str, Any] = {}

    def mutate(quizzes: Dict[str, Dict[str, Any]]) -> Any:
        block = quizzes.get(str(quiz_id))
        claim = (block or {}).get("secret_save") or {}
        if (not block or claim.get("request_id") != request_id or claim.get("state") != SAVE_WRITING
                or block.get("state") not in ("open", "expired_terminal")):
            result.update(error="secret_claim_lost", block=dict(block or {}))
            return keep
        stamp = utc_now_iso()
        request = secret_request_of(block) or {}
        late = block.get("state") == "expired_terminal"
        block.update(state="answered", answered_at=stamp, request_id=request_id, answer_source="web",
                     delivery=delivery_debt(task_id, quiz_id, "answer"),
                     secret_save={**claim, "state": SAVE_SAVED, "saved_at": stamp},
                     secret_outcome={"event": EVENT_FILLED, "pluginName": request.get("plugin_name", ""),
                                     "fields": field_ids(request), "at": stamp,
                                     "settings_keys": settings_keys(request), **outcome_facts},
                     **({"answered_after_terminal": True} if late else {}))
        result.update(ok=True, block=dict(block))
        return True

    mutate_projection(drive_root, task_id, mutate)
    return result


def cancel_outcome(block: Dict[str, Any]) -> Dict[str, Any]:
    """The typed closure facts of an owner cancel, including an interrupted earlier save."""
    request = secret_request_of(block) or {}
    claim = block.get("secret_save") if isinstance(block.get("secret_save"), dict) else {}
    return {"event": EVENT_CANCELLED, "pluginName": request.get("plugin_name", ""), "fields": field_ids(request),
            "at": utc_now_iso(), "settings_keys": settings_keys(request),
            **({"interrupted_save": True} if claim.get("state") == SAVE_WRITING else {})}


def grant_facts(drive_root: Any, request: Dict[str, Any]) -> Dict[str, Any]:
    """What the skill's CURRENT grant lets it read of the saved keys (read-only, never a grant)."""
    try:
        from ouroboros.skill_loader import grant_status_for_skill

        skill = _find_skill(drive_root, str(request.get("plugin_name") or ""))
        status = grant_status_for_skill(drive_root, skill) if skill is not None else None
    except Exception:
        status = None
    if not isinstance(status, dict):
        return {"grant_unknown": True}
    if status.get("unsupported_for_skill_type"):
        return {"grant_missing": settings_keys(request), "grant_unsupported": True}
    missing = set(status.get("missing_keys") or [])
    return {"grant_missing": [key for key in settings_keys(request) if key in missing]}


def _event_line(block: Dict[str, Any]) -> str:
    event = public_event(block)
    return f"{event['event']}: " + json.dumps({"pluginName": event["pluginName"], "fields": event["fields"]},
                                             ensure_ascii=False)


def filled_frame(block: Dict[str, Any]) -> str:
    """The model's ``secret_filled`` message: plugin and field names, never a value."""
    outcome = block.get("secret_outcome") or {}
    request = secret_request_of(block) or {}
    keys = ", ".join(settings_keys(request))
    missing = list(outcome.get("grant_missing") or [])
    if outcome.get("grant_unknown"):
        grant = "Whether the skill may read the saved key now could not be read."
    elif outcome.get("grant_unsupported"):
        grant = "This skill type cannot receive Settings keys at all."
    elif missing:
        grant = (f"The skill cannot read {', '.join(missing)} until the owner grants it in Skills; "
                 "this save granted nothing.")
    else:
        grant = "The skill's current grant covers the saved key."
    return "\n".join([
        f"[Owner secret request] quiz {block.get('quiz_id')} — asked {block.get('asked_at')}, "
        f"saved {block.get('answered_at')}.",
        _event_line(block),
        f"Secret filled: {request.get('plugin_name')}, {', '.join(field_ids(request))}. The owner entered "
        f"the value in a protected form and the host saved it to Settings ({keys}); it is not shown to you "
        "and is not in this conversation, so do not ask for it again.",
        f"Saved is not connected: {grant} Nothing was enabled, reloaded or contacted by this save; "
        "continue the setup and check the integration's own status.",
    ])


def cancelled_frame(block: Dict[str, Any]) -> str:
    """The model's distinct cancellation message: what is still missing, and what is unknown."""
    request = secret_request_of(block) or {}
    names, keys = ", ".join(field_ids(request)), ", ".join(settings_keys(request))
    if (block.get("secret_outcome") or {}).get("interrupted_save"):
        saved = (f"The owner cancelled {request.get('plugin_name')}: {names}. The cancel itself saved "
                 "nothing, but an earlier save attempt was interrupted before it was confirmed, so whether "
                 f"its value reached Settings ({keys}) is unknown; do not assume it is there or absent.")
    else:
        saved = (f"The owner cancelled; nothing was saved for {request.get('plugin_name')}: {names} "
                 f"({keys} unchanged by this request).")
    return "\n".join([
        f"[Owner secret request cancelled] quiz {block.get('quiz_id')} — closed {block.get('closed_at')}.",
        _event_line(block), saved,
        "[Host note] The task can tell the owner what is still missing to continue and ask again only "
        "if they want to.",
    ])


__all__ = [
    "CANCEL_REASON", "CANCEL_REASON_INTERRUPTED", "EVENT_CANCELLED", "EVENT_FILLED", "MAX_SECRET_VALUE_CHARS",
    "SecretRequestError", "cancel_outcome", "cancelled_frame", "claim_save",
    "declared_secret_fields", "field_ids", "filled_frame", "finalize_save", "grant_facts", "public_event",
    "question_text", "release_save", "resolve_secret_request", "secret_request_of", "settings_keys",
    "stale_reason", "validate_values",
]
