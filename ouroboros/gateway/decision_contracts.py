"""Typed owner decision families; runtime validation stays at their ingresses."""

from __future__ import annotations

from typing import Any, Dict, Optional

try:
    from typing import Literal, NotRequired, TypedDict
except ImportError:  # pragma: no cover - Python 3.10 compatibility.
    from typing_extensions import Literal, NotRequired, TypedDict


class DecisionRequest(TypedDict):
    """POST /api/decisions body — the ONE answer ingress for owner decision
    cards (owner decision 1=A). ``decision_id`` is a composed family id:
    ``quiz:{task_id}:{quiz_id}``, ``routing:{client_message_id}:{routing_token}``,
    ``model_wait:{task_id}:{wait_id}``, ``card:{card_id}:{action_id}`` (a press on a
    living card; ``gateway/card_decision.py``), or the reserved
    ``interaction:{task_id}:{run_id}:{interaction_id}`` family.
    ``request_id`` is the idempotency key; a replayed request returns the
    recorded confirmation instead of acting twice. ``comment`` is the owner's
    optional verbatim remark on quiz/routing decisions.

    Quiz ``action=close`` instead requires a non-empty ``reason`` and conveys
    neither an answer nor approval; ``action=dismiss`` carries neither and only removes
    an accepted late answer of unknown delivery from current questions (never delivered,
    never resent). ``option_index`` is optional for answers: an owner who takes none
    of the offered options answers with a non-empty comment and no index.
    Routing requires the integer — its choice IS the option. Model waits
    instead require revision and action, with the matching action fields;
    persist_role defaults false, and an empty credential profile means Auto.
    A card press carries ``revision`` (the card revision it was drawn from; a newer card
    answers 409 ``card_stale`` with the current ``revision``) and ``values`` only for a
    form action (keys are the form's field names; JSON scalars, text at most 4000
    characters, at most 50).
    Each ingress validates its own closed field set before effects.
    """

    request_id: str
    decision_id: str
    option_index: NotRequired[int]
    comment: NotRequired[str]
    revision: NotRequired[int]
    action: NotRequired[Literal["auto_continue", "switch", "retry", "close", "dismiss"]]
    reason: NotRequired[str]  # explicit quiz closure; never an answer/comment
    auto_continue: NotRequired[bool]
    model: NotRequired[str]
    credential_profile_id: NotRequired[str]
    use_local: NotRequired[bool]
    persist_role: NotRequired[bool]
    values: NotRequired[Dict[str, Any]]  # card: form field values


class DecisionResponse(TypedDict, total=False):
    """Answer-ingress reply. 2xx carries the card's lifecycle state
    (quiz: answered or explicitly closed; duplicate marks an idempotent replay).
    Closure carries actor, request identity, reason and time plus its delivery
    disposition; an owed delivery remains canonical until recovery confirms it.
    A LATE quiz answer
    — the card's task already finished — is also 2xx, adding
    answered_after_terminal and forwarded: the answer is recorded and delivered
    into the card's chat as an ordinary owner message, and forwarded=false with
    a reason_code says when that chat is a machine or hidden destination with no
    owner turn to start. 409 carries the winning answered/closed disposition (and
    for a non-root addressee). Routing adds dispatched
    (confirmed durable receipt), task_id (derived promoted id), latest_status
    (superseding status on 409), reason/detail diagnostics, and cause (the
    owner-facing sentence for a refused routing act). A New task picked from a
    card that carried an explicit start adds reasoning_effort, read from the
    admitted root's own row; a steered existing task never reports one.

    Model waits distinguish accepted (202, applied false) from the worker's
    applied_request_id. wait carries the current projection and revision;
    saved says whether the optional permanent role update landed, with null
    reserved for the existing settings-writer timeout's unknown outcome.
    Applied, saved and a refusal or failure stay separate facts: an error after
    the claim still reports the claim's current row and receipt. ``applied:false``
    is no observed application yet, not proof that a pending action cannot apply. The role saves
    only while the task's stop state reads clear — a recorded Stop intent (Wrap
    up included) or Panic saves nothing, nor does an unreadable (unknown) state.

    A card press is recorded per ``request_id`` (each press its own; ``once``
    actions keep the first and answer 409 ``card_action_once`` with ``winner`` (its request_id,
    time and delivery: a winner still owed is retried under its request_id); a card
    that changed since it was drawn answers 409 ``card_stale`` with its ``revision``).
    ``delivery`` says where it went: ``delivered`` into the authoring task's
    mailbox, ``forwarded`` as the owner's message after that task ended, or
    ``owed`` (202 with ``reason_code``; resend the same ``request_id``).
    """

    ok: bool
    decision_id: str
    state: str
    answered_index: int
    comment: str
    closed_reason: str
    closed_at: str
    closed_actor: Literal["owner", "model"]
    closed_request_id: str
    question_delivery: Dict[str, Any]
    duplicate: bool
    answered_after_terminal: bool
    forwarded: bool
    error: str
    dispatched: str
    task_id: str
    latest_status: str
    reason: str
    detail: str
    cause: str
    reasoning_effort: str
    request_id: str
    applied: bool
    saved: Optional[bool]
    wait: Dict[str, Any]
    reason_code: str
    delivery: Literal["delivered", "forwarded", "owed"]
    card_id: str
    revision: int
    winner: Dict[str, Any]


class SecretRequestField(TypedDict):
    """One asked field: the manifest's public id and the Settings key it writes."""

    id: str
    setting_key: str


class SecretRequestMeta(TypedDict):
    """An integration-secret request's metadata on its quiz block and card (no value, ever).

    ``plugin_name`` is the installed skill; ``fields`` come from its reviewed ``secret_fields``
    manifest mapping; ``content_hash`` binds the request to the skill revision that declared it."""

    plugin_name: str
    fields: list[SecretRequestField]
    content_hash: str


class SecretOutcome(TypedDict):
    """The host's typed completion of a secret request: ``secret_filled`` after Settings kept the
    values, or ``secret_cancelled`` with nothing saved. ``grant_missing`` names saved keys the
    skill's current grant does not cover (saving grants nothing); ``interrupted_save`` marks a
    cancel after an earlier attempt whose result is unknown."""

    event: Literal["secret_filled", "secret_cancelled"]
    pluginName: str
    fields: list[str]
    at: str
    settings_keys: list[str]
    grant_missing: NotRequired[list[str]]
    grant_unknown: NotRequired[bool]
    grant_unsupported: NotRequired[bool]
    interrupted_save: NotRequired[bool]


class SecretEvent(TypedDict):
    """The screenshot's event payload: the plugin and field ids, never a value."""

    pluginName: str
    fields: list[str]


class SecretDecisionRequest(TypedDict):
    """POST /api/decisions/secret body — the browser's ``saveSecretToken``.

    ``decision_id`` is the request's question identity ``quiz:{task_id}:{quiz_id}``.
    ``values`` maps EXACTLY the asked field ids to the owner's entries and exists only on
    ``save`` (the default); ``action: "cancel"`` carries none. ``request_id`` is reused only
    for the same values: a changed entry is a new request."""

    request_id: str
    decision_id: str
    action: NotRequired[Literal["save", "cancel"]]
    values: NotRequired[Dict[str, str]]


class SecretDecisionResponse(TypedDict, total=False):
    """Secret form reply; ``saved`` is present on BOTH sides of the Settings commit.

    200 carries the recorded lifecycle: ``secret_filled`` or ``secret_cancelled`` (plugin and
    field ids), ``settings_keys``, the grant facts and the question delivery. A refusal says
    ``saved: false`` with ``reason_code`` and the request's current facts, so a losing tab
    settles on the winner. ``saved: true`` with ``secret_save_unverified`` or
    ``secret_completion_unrecorded`` is a written value not reported filled; ``saved: null``
    with ``secret_save_unconfirmed`` is an attempt of unknown outcome, never repeated under its
    own request. Either way read status first; only a new request saves again.
    ``save_unconfirmed`` marks an interrupted earlier attempt whose result is unknown."""

    ok: bool
    saved: Optional[bool]
    decision_id: str
    state: str
    duplicate: bool
    secret_filled: SecretEvent
    secret_cancelled: SecretEvent
    secret_outcome: SecretOutcome
    settings_keys: list[str]
    grant_missing: list[str]
    grant_unknown: bool
    grant_unsupported: bool
    interrupted_save: bool
    save_unconfirmed: bool
    closed_reason: str
    closed_at: str
    closed_actor: str
    closed_request_id: str
    closed_by_task_id: str
    question_delivery: Dict[str, Any]
    error: str
    reason_code: str
