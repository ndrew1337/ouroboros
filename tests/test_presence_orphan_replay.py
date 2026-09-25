"""An orphan-reconciled presence turn is neither spoken nor final: the retry answers it."""

from __future__ import annotations

import hashlib
import json
import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from ouroboros import agent_task_pipeline as pipeline
from ouroboros.gateway.host_service import create_host_service_app
from ouroboros.outcomes import infra_failed_axes
from ouroboros.presence_context import build_presence_context_section
from ouroboros.presence_runner import (
    PresenceTurnGate,
    _cached_result,
    _task_id,
    presence_result_from_stored,
    run_presence_turn,
)
from ouroboros.task_results import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_RUNNING,
    load_task_result,
    reopen_reconciled_presence_placeholder,
    task_result_path,
    write_task_result,
)
from ouroboros.task_status import reconcile_orphaned_running_tasks
from ouroboros.tools.presence import _finish_presence
from ouroboros.utils import append_jsonl
from tests.test_host_service_api import _seed_presence_behavior, _seed_token
from tests.test_presence_runner import _admission, _event

_PRESENCE_METADATA = {"source": "presence", "presence": {"binding_id": "1" * 32, "delivery_reporting_version": 0}}
_NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z, the fresh queue snapshot's own time


def _sweep(tmp_path, monkeypatch, task_id, status=STATUS_RUNNING, *, seed=True, boot="2026-05-28T00:00:02+00:00",
           metadata=_PRESENCE_METADATA, **fields):
    """The real reconciler: a stale row, a later worker boot, a fresh empty queue, one sweep."""
    with monkeypatch.context() as patch:
        patch.setattr(time, "time", lambda: _NOW)
        if seed:
            write_task_result(tmp_path, task_id, status, result="Task is running.",
                              ts="2026-05-28T00:00:00+00:00", metadata=dict(metadata), **fields)
        (tmp_path / "state").mkdir(exist_ok=True)
        (tmp_path / "state" / "queue_snapshot.json").write_text(
            '{"ts": "2027-01-15T08:00:00+00:00", "pending": [], "running": []}', encoding="utf-8")
        events = tmp_path / "logs" / "events.jsonl"
        append_jsonl(events, {"ts": "2026-05-28T00:00:01+00:00", "type": "llm_round", "task_id": task_id})
        append_jsonl(events, {"ts": boot, "type": "worker_boot"})
        healed = reconcile_orphaned_running_tasks(tmp_path)
    return healed, load_task_result(tmp_path, task_id)


def _reconciled(tmp_path, monkeypatch, task_id, status=STATUS_RUNNING, **kwargs):
    healed, row = _sweep(tmp_path, monkeypatch, task_id, status, **kwargs)
    assert healed == 1 and row["status"] == STATUS_FAILED and row["status_reconciled_from"] == status
    assert row["reason_code"] == ("interrupted_retry_lost" if status == STATUS_INTERRUPTED
                                  else "orphaned_running_after_worker_restart")
    assert "TASK_ORPHAN_RECONCILED" in row["result"] and not row.get("terminal_origin")
    return row


def test_reconciled_turn_replays_silent_while_completed_legacy_row_still_speaks(tmp_path, monkeypatch):
    row = _reconciled(tmp_path, monkeypatch, "orphan-turn")
    replay = presence_result_from_stored(row, "orphan-turn")
    assert replay.outcome == "silent" and replay.text == ""
    assert _cached_result(tmp_path, "orphan-turn") is None
    # The unknown-origin compatibility path itself survives for completed rows.
    legacy = presence_result_from_stored({"status": "completed", "result": "Old reply", "metadata": {}}, "old")
    assert legacy.outcome == "message" and legacy.text == "Old reply"


@pytest.mark.parametrize("status", [STATUS_RUNNING, STATUS_INTERRUPTED])
def test_reopen_moves_the_host_mark_aside_exactly_once(tmp_path, monkeypatch, status):
    row = _reconciled(tmp_path, monkeypatch, "orphan-turn", status)
    assert reopen_reconciled_presence_placeholder(tmp_path, "orphan-turn") is True
    reopened = load_task_result(tmp_path, "orphan-turn")
    assert reopened["status"] == STATUS_RUNNING and reopened["metadata"] == row["metadata"]
    assert reopened["superseded_placeholder"] == {
        "status": STATUS_FAILED, "reason_code": row["reason_code"], "status_reconciled_from": status,
        "ts": row["ts"], "result": row["result"][-500:],
        # the failed transition's terminal-projection provenance goes aside with the mark
        "canonical_terminal_projection_origin": "terminal_transition",
    }
    for cleared in ("reason_code", "outcome_axes", "artifact_status", "artifact_bundle", "result",
                    "status_reconciled_from", "canonical_terminal_projection_origin"):
        assert cleared not in reopened
    # A running row is no placeholder: the transition cannot fire twice.
    before = task_result_path(tmp_path, "orphan-turn").read_bytes()
    assert reopen_reconciled_presence_placeholder(tmp_path, "orphan-turn") is False
    assert task_result_path(tmp_path, "orphan-turn").read_bytes() == before


def _outcome_failure(tmp_path, monkeypatch):
    healed, row = _sweep(tmp_path, monkeypatch, "axes-turn", reason_code="provider_unavailable",
                         outcome_axes=infra_failed_axes("provider_unavailable"))
    # task_status: a RUNNING row whose own axes already failed is marked from those axes.
    assert healed == 1 and row["status"] == STATUS_FAILED and row["status_reconciled_from"] == STATUS_RUNNING
    assert row["reason_code"] == "provider_unavailable" and "TASK_ORPHAN_RECONCILED" not in row["result"]
    return "axes-turn"


def _ordinary_failure(tmp_path, _monkeypatch):
    write_task_result(tmp_path, "failed-turn", STATUS_FAILED, result="Authored partial reply",
                      terminal_origin="model_final", reason_code="round_limit",
                      metadata={**_PRESENCE_METADATA, "presence_outcome": "message"})
    return "failed-turn"


def _non_presence_orphan(tmp_path, monkeypatch):
    _reconciled(tmp_path, monkeypatch, "ordinary-task", metadata={"source": "web"})
    return "ordinary-task"


@pytest.mark.parametrize("seed", [_outcome_failure, _ordinary_failure, _non_presence_orphan])
def test_only_the_orphan_placeholder_of_a_presence_turn_reopens(tmp_path, monkeypatch, seed):
    task_id = seed(tmp_path, monkeypatch)
    before = task_result_path(tmp_path, task_id).read_bytes()
    assert reopen_reconciled_presence_placeholder(tmp_path, task_id) is False
    assert task_result_path(tmp_path, task_id).read_bytes() == before
    if seed is not _non_presence_orphan:
        assert _cached_result(tmp_path, task_id) is not None  # a real terminal replays
    write_task_result(tmp_path, task_id, STATUS_COMPLETED, result="Late answer")
    assert load_task_result(tmp_path, task_id)["status"] == STATUS_FAILED  # sticky terminal unchanged


def _answering_agent(calls, reply, drive_root, *, during=None, lost=False):
    """Real durable pipeline: the RUNNING start write, then the terminal write (or a lost worker)."""

    class Agent:
        def handle_task(self, task):
            calls.append(task)
            task["_skip_post_task_synthesis"] = True
            write_task_result(drive_root, task["id"], STATUS_RUNNING, metadata=task["metadata"],
                              result="Task is running.", ts="2026-05-28T00:00:00+00:00" if lost else "")
            if during is not None:
                during(task)
            if lost:
                raise RuntimeError("worker lost")
            ctx = SimpleNamespace(task_contract=task["task_contract"], task_metadata=task["metadata"])
            _finish_presence(ctx, "message", reply)
            ctx._presence_completion_accepted = True
            pending: list = []
            pipeline.emit_task_results(
                SimpleNamespace(drive_root=drive_root, repo_dir=drive_root), None, None, pending, task, reply,
                {"terminal_origin": "model_final"}, {"tool_calls": [], "reasoning_notes": []}, 0.0,
                drive_root / "logs", ctx=ctx,
            )
            return pending

    return Agent()


def test_reconciled_turn_is_not_cached_and_its_rerun_persists(tmp_path, monkeypatch):
    task_id = _task_id(_admission(), _event())
    _reconciled(tmp_path, monkeypatch, task_id)
    assert _cached_result(tmp_path, task_id) is None
    calls: list = []
    kwargs = dict(admission=_admission(), event=_event(), repo_dir=tmp_path, drive_root=tmp_path,
                  agent_factory=lambda **_kw: _answering_agent(calls, "Real answer", tmp_path),
                  gate=PresenceTurnGate(1))
    first = run_presence_turn(**kwargs)
    assert [task["id"] for task in calls] == [task_id] and first.outcome == "message" and first.text == "Real answer"
    stored = load_task_result(tmp_path, task_id)
    assert stored["status"] == STATUS_COMPLETED and stored["terminal_origin"] == "model_final"
    assert "status_reconciled_from" not in stored and "TASK_ORPHAN_RECONCILED" not in stored["result"]
    assert stored["superseded_placeholder"]["reason_code"] == "orphaned_running_after_worker_restart"
    # A v0 transport reports no receipts: what the lost attempt sent is unknown, and the model is told so.
    attempt = calls[0]["metadata"]["presence"]["previous_attempt"]
    assert attempt == {"delivered_count": None, "delivered": None, "uncertain_count": 0}
    section = build_presence_context_section(tmp_path, calls[0]["metadata"]["presence"])
    assert "whether it already sent anything is unknown" in section
    # The persisted answer is now the cached result: a further retry does not re-run.
    assert run_presence_turn(**kwargs) == first and len(calls) == 1


def test_reconciler_repersist_keeps_the_placeholder(tmp_path, monkeypatch):
    row = _reconciled(tmp_path, monkeypatch, "orphan-turn")
    write_task_result(tmp_path, "orphan-turn", STATUS_FAILED, result=row["result"],
                      status_reconciled_from=row["status_reconciled_from"])
    assert load_task_result(tmp_path, "orphan-turn")["status_reconciled_from"] == STATUS_RUNNING
    assert _cached_result(tmp_path, "orphan-turn") is None


def test_ordinary_failed_turn_still_replays_and_stays_failed(tmp_path):
    task_id = _task_id(_admission(), _event())
    write_task_result(tmp_path, task_id, STATUS_FAILED, result="Authored partial reply",
                      terminal_origin="model_final", reason_code="round_limit",
                      metadata={**_PRESENCE_METADATA, "presence_outcome": "message",
                                "presence_result_text": "Authored partial reply"})
    calls: list = []
    result = run_presence_turn(admission=_admission(), event=_event(), repo_dir=tmp_path, drive_root=tmp_path,
                               agent_factory=lambda **_kw: _answering_agent(calls, "New answer", tmp_path),
                               gate=PresenceTurnGate(1))
    assert calls == [] and result.outcome == "message" and result.text == "Authored partial reply"
    write_task_result(tmp_path, task_id, STATUS_COMPLETED, result="New answer")
    assert load_task_result(tmp_path, task_id)["status"] == STATUS_FAILED
    # A failed row whose origin is unknown is host text, never a reply.
    write_task_result(tmp_path, "unknown-origin", STATUS_FAILED, result="Error during processing",
                      metadata=dict(_PRESENCE_METADATA))
    assert presence_result_from_stored(load_task_result(tmp_path, "unknown-origin"), "unknown-origin").text == ""


@pytest.mark.parametrize("origin", ["", "host_notice", "host_salvage"])
def test_failed_deferred_turn_keeps_its_work_reference(tmp_path, origin):
    write_task_result(tmp_path, "deferred-turn", STATUS_FAILED, result="Host diagnostic", terminal_origin=origin,
                      metadata={**_PRESENCE_METADATA, "presence_outcome": "deferred",
                                "presence_work_ref": "presence-work-1"})
    replay = _cached_result(tmp_path, "deferred-turn")
    assert (replay.outcome, replay.text, replay.work_ref) == ("deferred", "", "presence-work-1")


def test_reconciled_non_presence_task_keeps_the_sticky_terminal(tmp_path, monkeypatch):
    _reconciled(tmp_path, monkeypatch, "ordinary-task", metadata={"source": "web"})
    write_task_result(tmp_path, "ordinary-task", STATUS_COMPLETED, result="Late answer")
    row = load_task_result(tmp_path, "ordinary-task")
    assert row["status"] == STATUS_FAILED and row["status_reconciled_from"] == STATUS_RUNNING


def test_host_retry_reruns_a_lost_turn_once_and_then_replays(tmp_path, monkeypatch):
    """Through POST /presence/turn: a lost v1 attempt, the reconciler, the re-run, a replay."""
    _seed_token(tmp_path, skill="telegram-bot", token="presence-token",
                permissions=["presence"], manifest_permissions=["presence"])
    binding_id = _seed_presence_behavior(tmp_path)
    task_id = "presence-" + hashlib.sha256(f"{binding_id}\0telegram:bot-1:42".encode("utf-8")).hexdigest()[:24]
    agents: list = []

    def run_real_presence(**kwargs):
        return run_presence_turn(repo_dir=tmp_path, drive_root=tmp_path, agent_factory=lambda **_kw: agents.pop(0),
                                 gate=PresenceTurnGate(1), **kwargs)

    client = TestClient(create_host_service_app(tmp_path, presence_runner=run_real_presence))
    recorder = client.app.state.host_service_context.presence_deliveries

    def early_send(task):  # the lost attempt had already delivered one transport message
        recorder.record("telegram-bot", {
            "schema_version": 1, "delivery_id": "send:early", "part_id": "0", "state": "delivered",
            "provider": "telegram", "account_id": "bot-1", "conversation_id": "room-1", "thread_id": "topic-1",
            "text": "Early part", "format": "markdown", "message": {"provider_message_id": "501"},
            "origin": {"kind": "tool", "task_id": task["id"], "source_event_id": "telegram:bot-1:42"},
        })

    def post():
        return client.post("/presence/turn", headers={"X-Skill-Token": "presence-token"}, json={
            "binding_id": binding_id, "delivery_reporting_version": 1, "event": {
                "source_event_id": "telegram:bot-1:42", "provider": "telegram", "account_id": "bot-1",
                "conversation_id": "room-1", "thread_id": "topic-1", "conversation_key": "ignored",
                "actor": {"platform_actor_id": "user-7"}, "conversation": {}, "message": {"message_id": "42"},
                "text": "Hello",
            }})

    calls: list = []
    agents.append(_answering_agent(calls, "", tmp_path, during=early_send, lost=True))
    assert post().status_code == 500 and load_task_result(tmp_path, task_id)["status"] == STATUS_RUNNING
    _reconciled(tmp_path, monkeypatch, task_id, seed=False)

    sweeps = []

    def sweep_during_rerun(_task):  # stale by clock and by a later boot, but executing in this process
        sweeps.append(_sweep(tmp_path, monkeypatch, task_id, seed=False, boot="2027-01-15T07:59:00+00:00")[0])
        assert load_task_result(tmp_path, task_id)["status"] == STATUS_RUNNING

    agents.append(_answering_agent(calls, "Real answer", tmp_path, during=sweep_during_rerun))
    first = post()
    assert first.status_code == 200 and first.json()["text"] == "Real answer" and first.json()["turn_ref"] == task_id
    assert sweeps == [0] and len(calls) == 2
    assert calls[1]["metadata"]["presence"]["previous_attempt"] == {
        "delivered_count": 1, "delivered": ["Early part"], "uncertain_count": 0}
    section = build_presence_context_section(tmp_path, calls[1]["metadata"]["presence"])
    assert 'already delivered 1 message(s): "Early part"' in section
    stored = load_task_result(tmp_path, task_id)
    assert stored["status"] == STATUS_COMPLETED and "status_reconciled_from" not in stored
    assert stored["superseded_placeholder"]["status"] == STATUS_FAILED
    rows = [json.loads(line) for line in (tmp_path / "logs" / "chat.jsonl").read_text(encoding="utf-8").splitlines()]
    inbound = [row for row in rows if row.get("direction") == "in" and row.get("client_message_id") == "telegram:bot-1:42"]
    assert len(inbound) == 1  # the re-run does not log the correspondent's message twice
    replay = post()
    assert replay.status_code == 200 and replay.json() == first.json() and len(calls) == 2


def _lost_v1_attempt(tmp_path, task_id, *, chat_id):
    """A v1 attempt that logged its inbound row, delivered one part through a tool, then died."""
    write_task_result(tmp_path, task_id, STATUS_RUNNING, result="Task is running.", ts="2026-05-28T00:00:00+00:00",
                      metadata={"source": "presence", "presence": {"binding_id": "1" * 32, "delivery_reporting_version": 1}})
    chat = tmp_path / "logs" / "chat.jsonl"
    append_jsonl(chat, {"direction": "in", "chat_id": chat_id, "client_message_id": "telegram:bot-1:42",
                        "text": "Hello", "task_id": task_id})
    append_jsonl(chat, {"type": "presence_delivery", "direction": "out", "chat_id": chat_id, "text": "Early part",
                        "task_id": task_id, "transport": {"conversation_key": _event().conversation_key, "delivery": {
                            "state": "delivered", "delivery_id": "send:early", "part_id": "0"}}})
    return chat


def _v1_kwargs(tmp_path, calls, reply="Real answer", **overrides):
    from dataclasses import replace

    return dict(admission=_admission(), event=replace(_event(), delivery_reporting_version=1), repo_dir=tmp_path,
                drive_root=tmp_path, agent_factory=lambda **_kw: _answering_agent(calls, reply, tmp_path),
                gate=PresenceTurnGate(1), **overrides)


def test_stale_running_row_is_a_lost_attempt_before_the_reconciler_runs(tmp_path):
    """An adapter retry inside the reconciler's grace window still learns what the dead attempt sent."""
    from dataclasses import replace

    task_id = _task_id(_admission(), _event())
    chat_id = 0
    calls: list = []
    kwargs = _v1_kwargs(tmp_path, calls)
    fresh = run_presence_turn(**{**kwargs, "event": replace(kwargs["event"], source_event_id="telegram:bot-1:1")})
    assert fresh.text == "Real answer" and "previous_attempt" not in calls[0]["metadata"]["presence"]
    chat_id = calls[0]["chat_id"]
    _lost_v1_attempt(tmp_path, task_id, chat_id=chat_id)
    assert _cached_result(tmp_path, task_id) is None
    first = run_presence_turn(**kwargs)
    assert first.text == "Real answer" and [task["id"] for task in calls] == [calls[0]["id"], task_id]
    assert calls[1]["metadata"]["presence"]["previous_attempt"] == {
        "delivered_count": 1, "delivered": ["Early part"], "uncertain_count": 0}
    stored = load_task_result(tmp_path, task_id)
    assert stored["status"] == STATUS_COMPLETED and "superseded_placeholder" not in stored  # nothing to reopen
    rows = [json.loads(line) for line in (tmp_path / "logs" / "chat.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(1 for row in rows if row.get("direction") == "in" and row.get("task_id") == task_id) == 1
    assert run_presence_turn(**kwargs) == first and len(calls) == 2


def test_rotation_between_the_lost_attempt_and_its_retry_makes_prior_sends_unknown(tmp_path):
    """Receipts in a rotated archive are not counted as zero: the model is told the count is unknown."""
    task_id = _task_id(_admission(), _event())
    chat = _lost_v1_attempt(tmp_path, task_id, chat_id=7)
    (tmp_path / "archive").mkdir()
    chat.rename(tmp_path / "archive" / "chat_20260528T000100.jsonl")  # the live generation rotated
    calls: list = []
    first = run_presence_turn(**_v1_kwargs(tmp_path, calls))
    assert first.text == "Real answer" and len(calls) == 1
    assert calls[0]["metadata"]["presence"]["previous_attempt"] == {
        "delivered_count": None, "delivered": None, "uncertain_count": 0}
    section = build_presence_context_section(tmp_path, calls[0]["metadata"]["presence"])
    assert "whether it already sent anything is unknown" in section and "delivered 0 message" not in section
    live = [json.loads(line) for line in chat.read_text(encoding="utf-8").splitlines()] if chat.exists() else []
    assert not [row for row in live if row.get("task_id") == task_id and row.get("direction") == "in"]  # never re-logged


def test_a_second_death_after_the_rotation_keeps_the_count_unknown(tmp_path):
    """The retry that follows a rotation must not leave a fresh inbound row that a later retry mistakes
    for complete receipt coverage: the archived send stays unknown, never zero."""
    task_id = _task_id(_admission(), _event())
    chat = _lost_v1_attempt(tmp_path, task_id, chat_id=7)
    (tmp_path / "archive").mkdir()
    chat.rename(tmp_path / "archive" / "chat_20260528T000100.jsonl")
    calls: list = []
    with pytest.raises(RuntimeError):  # retry A dies after its running write
        run_presence_turn(**{**_v1_kwargs(tmp_path, calls),
                             "agent_factory": lambda **_kw: _answering_agent(calls, "", tmp_path, lost=True)})
    second = run_presence_turn(**_v1_kwargs(tmp_path, calls))  # retry B
    assert second.text == "Real answer" and len(calls) == 2
    assert [task["metadata"]["presence"]["previous_attempt"] for task in calls] == [
        {"delivered_count": None, "delivered": None, "uncertain_count": 0}] * 2


def test_rejected_build_leaves_the_placeholder_for_the_next_retry(tmp_path, monkeypatch):
    """The host mark moves aside only when the turn actually runs; a rejected build keeps it."""
    from ouroboros.presence_runner import PresenceTurnError
    from ouroboros.task_results import is_reconciled_presence_placeholder

    task_id = _task_id(_admission(), _event())
    _reconciled(tmp_path, monkeypatch, task_id)
    calls: list = []
    kwargs = dict(admission=_admission(), event=_event(), repo_dir=tmp_path, drive_root=tmp_path,
                  agent_factory=lambda **_kw: _answering_agent(calls, "Real answer", tmp_path), gate=PresenceTurnGate(1))
    with pytest.raises(PresenceTurnError):
        run_presence_turn(**kwargs, staged_files=[tmp_path / "missing.png"])
    stored = load_task_result(tmp_path, task_id)
    assert is_reconciled_presence_placeholder(stored) and calls == []  # still the placeholder, not a phantom running row
    first = run_presence_turn(**kwargs)
    assert first.text == "Real answer" and "previous_attempt" in calls[0]["metadata"]["presence"]
    stored = load_task_result(tmp_path, task_id)
    assert stored["status"] == STATUS_COMPLETED and stored["superseded_placeholder"]["status"] == STATUS_FAILED


def test_uncertain_receipts_make_the_prior_count_a_floor(tmp_path):
    """A timed-out send the provider never confirmed is neither counted nor forgotten."""
    task_id = _task_id(_admission(), _event())
    chat = _lost_v1_attempt(tmp_path, task_id, chat_id=7)
    append_jsonl(chat, {"type": "presence_delivery", "direction": "system", "chat_id": 7, "text": "Maybe part",
                        "task_id": task_id, "transport": {"conversation_key": _event().conversation_key, "delivery": {
                            "state": "uncertain", "delivery_id": "send:late", "part_id": "0"}}})
    calls: list = []
    run_presence_turn(**_v1_kwargs(tmp_path, calls))
    attempt = calls[0]["metadata"]["presence"]["previous_attempt"]
    assert attempt == {"delivered_count": 1, "delivered": ["Early part"], "uncertain_count": 1}
    section = build_presence_context_section(tmp_path, calls[0]["metadata"]["presence"])
    assert 'delivered at least 1 message(s): "Early part"; 1 more part(s) may have landed' in section


@pytest.mark.parametrize("states, uncertain", [(("uncertain", "failed"), 0), (("failed", "uncertain"), 1)])
def test_a_part_settles_by_its_latest_receipt(tmp_path, states, uncertain):
    """A timed-out part the provider later refused is neither delivered nor possibly landed; a refused
    part whose retry timed out may have landed after all."""
    task_id = _task_id(_admission(), _event())
    chat = _lost_v1_attempt(tmp_path, task_id, chat_id=7)
    for state in states:
        append_jsonl(chat, {"type": "presence_delivery", "direction": "system", "chat_id": 7, "text": "Maybe part",
                            "task_id": task_id, "transport": {"conversation_key": _event().conversation_key, "delivery": {
                                "state": state, "delivery_id": "send:late", "part_id": "0"}}})
    calls: list = []
    run_presence_turn(**_v1_kwargs(tmp_path, calls))
    attempt = calls[0]["metadata"]["presence"]["previous_attempt"]
    assert attempt == {"delivered_count": 1, "delivered": ["Early part"], "uncertain_count": uncertain}
    section = build_presence_context_section(tmp_path, calls[0]["metadata"]["presence"])
    assert ("may have landed" in section) is bool(uncertain)


def test_an_attempt_that_died_before_its_running_write_still_logs_the_message_once(tmp_path):
    """Only the inbound row survives such a death; the retry must not repeat the correspondent."""
    calls: list = []
    kwargs = _v1_kwargs(tmp_path, calls)
    agents = [None, _answering_agent(calls, "Real answer", tmp_path)]

    def factory(**_kw):
        agent = agents.pop(0)
        if agent is None:
            raise RuntimeError("worker died before the running write")
        return agent

    with pytest.raises(RuntimeError):
        run_presence_turn(**{**kwargs, "agent_factory": factory})
    first = run_presence_turn(**{**kwargs, "agent_factory": factory})
    assert first.text == "Real answer" and "previous_attempt" not in calls[0]["metadata"]["presence"]
    rows = [json.loads(line) for line in (tmp_path / "logs" / "chat.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["direction"] for row in rows if row.get("task_id") == first.task_id].count("in") == 1


def test_a_confirmed_part_later_refused_is_not_delivered(tmp_path):
    """One latest-state rule for confirmed and uncertain parts alike."""
    task_id = _task_id(_admission(), _event())
    chat = _lost_v1_attempt(tmp_path, task_id, chat_id=7)
    append_jsonl(chat, {"type": "presence_delivery", "direction": "system", "chat_id": 7, "text": "Early part",
                        "task_id": task_id, "transport": {"conversation_key": _event().conversation_key, "delivery": {
                            "state": "failed", "delivery_id": "send:early", "part_id": "0"}}})
    calls: list = []
    run_presence_turn(**_v1_kwargs(tmp_path, calls))
    assert calls[0]["metadata"]["presence"]["previous_attempt"] == {
        "delivered_count": 0, "delivered": [], "uncertain_count": 0}


def test_a_receipt_addressed_to_another_conversation_does_not_count(tmp_path):
    """A tool send elsewhere with the same body is not this conversation's delivery."""
    task_id = _task_id(_admission(), _event())
    chat = _lost_v1_attempt(tmp_path, task_id, chat_id=7)
    append_jsonl(chat, {"type": "presence_delivery", "direction": "out", "chat_id": 8, "text": "Early part",
                        "task_id": task_id, "transport": {"conversation_key": "telegram:bot-1:other-room:0", "delivery": {
                            "state": "delivered", "delivery_id": "send:elsewhere", "part_id": "0"}}})
    calls: list = []
    run_presence_turn(**_v1_kwargs(tmp_path, calls))
    assert calls[0]["metadata"]["presence"]["previous_attempt"] == {
        "delivered_count": 1, "delivered": ["Early part"], "uncertain_count": 0}


def test_reconciler_skips_a_row_whose_retry_went_live_after_the_decision(tmp_path, monkeypatch):
    """The orphan decision is taken outside the row lock; a presence retry that registered meanwhile
    cancels the write, and the same sweep heals once nothing is live."""
    from ouroboros import presence_runner, task_status

    task_id = "presence-raced"
    real_effective, order = task_status.load_effective_task_result, []

    def effective_then_retry_registers(root, tid, *args, **kwargs):
        effective = real_effective(root, tid, *args, **kwargs)
        if tid == task_id and not order:  # the retry goes live right after the sweep decided
            order.append("live")
            with presence_runner._LIVE_LOCK:
                presence_runner._LIVE_PRESENCE_TASKS.add(task_id)
        return effective

    monkeypatch.setattr(task_status, "load_effective_task_result", effective_then_retry_registers)
    try:
        healed, row = _sweep(tmp_path, monkeypatch, task_id)
        assert (healed, row["status"], order) == (0, STATUS_RUNNING, ["live"])  # decision dropped, row untouched
    finally:
        with presence_runner._LIVE_LOCK:
            presence_runner._LIVE_PRESENCE_TASKS.discard(task_id)
    healed, row = _sweep(tmp_path, monkeypatch, task_id, seed=False)
    assert healed == 1 and row["status"] == STATUS_FAILED and row["status_reconciled_from"] == STATUS_RUNNING


def test_reconciler_settles_nothing_when_the_row_was_requeued_after_the_decision(tmp_path, monkeypatch):
    """A row requeued (scheduled) between the decision and the write is neither healed nor cleaned up."""
    from ouroboros import owner_quiz, task_status

    task_id = "presence-requeued"
    real_effective, cleanups = task_status.load_effective_task_result, []

    def effective_then_requeue(root, tid, *args, **kwargs):
        effective = real_effective(root, tid, *args, **kwargs)
        if tid == task_id:
            write_task_result(tmp_path, task_id, "scheduled", result="New authority")
        return effective

    monkeypatch.setattr(task_status, "load_effective_task_result", effective_then_requeue)
    monkeypatch.setattr(owner_quiz, "reconcile_terminal", lambda root, tid: cleanups.append(tid))
    healed, row = _sweep(tmp_path, monkeypatch, task_id)
    assert (healed, row["status"], row["result"], cleanups) == (0, "scheduled", "New authority", [])
    # The same sweep over a genuine orphan still heals and still runs the terminal cleanup.
    monkeypatch.setattr(task_status, "load_effective_task_result", real_effective)
    healed, row = _sweep(tmp_path, monkeypatch, "presence-orphan")
    assert (healed, row["status"], cleanups) == (1, STATUS_FAILED, ["presence-orphan"])


def test_an_unwritable_inbound_row_fails_the_turn_before_the_model_runs(tmp_path, monkeypatch):
    """The no-re-log rule assumes the inbound row landed; a failed append is a failed turn, then a retry logs it."""
    from ouroboros import presence_runner
    from ouroboros.presence_runner import PresenceTurnError

    calls: list = []
    kwargs = _v1_kwargs(tmp_path, calls)
    real_append = presence_runner.append_jsonl
    monkeypatch.setattr(presence_runner, "append_jsonl", lambda path, obj=None, **_kw: False)
    with pytest.raises(PresenceTurnError) as raised:
        run_presence_turn(**kwargs)
    assert raised.value.code == "chat_log_unwritable" and calls == []
    assert load_task_result(tmp_path, _task_id(_admission(), kwargs["event"])) is None  # no lost attempt to inherit
    monkeypatch.setattr(presence_runner, "append_jsonl", real_append)
    first = run_presence_turn(**kwargs)
    rows = [json.loads(line) for line in (tmp_path / "logs" / "chat.jsonl").read_text(encoding="utf-8").splitlines()]
    assert first.text == "Real answer" and [r["direction"] for r in rows if r.get("task_id") == first.task_id] == ["in"]


def _sending_agent(calls, reply, drive_root, *, part, rotate_first=False):
    """A real-pipeline agent whose turn records one delivered receipt for this conversation mid-turn."""
    def send(task):
        chat = drive_root / "logs" / "chat.jsonl"
        if rotate_first:  # the live generation rotates while the turn runs
            (drive_root / "archive").mkdir(exist_ok=True)
            chat.rename(drive_root / "archive" / "chat_20260528T000200.jsonl")
        append_jsonl(chat, {"type": "presence_delivery", "direction": "out", "chat_id": task["chat_id"], "text": part,
                            "task_id": task["id"], "transport": {"conversation_key": _event().conversation_key,
                                                                 "delivery": {"state": "delivered", "delivery_id": f"send:{part}", "part_id": "0"}}})
    return _answering_agent(calls, reply, drive_root, during=send)


def test_a_retry_after_a_rotation_still_knows_its_own_sends(tmp_path):
    """The lost attempt's archived sends stay unknown to the retry, but the retry's own receipts, which
    all landed in the generation live when it started, are its pointer's confirmed sends."""
    from ouroboros.presence_runner import _read_previous_turn

    task_id = _task_id(_admission(), _event())
    chat = _lost_v1_attempt(tmp_path, task_id, chat_id=7)
    (tmp_path / "archive").mkdir()
    chat.rename(tmp_path / "archive" / "chat_20260528T000100.jsonl")
    chat.touch()  # the rotator leaves a fresh live generation behind, as in production
    calls: list = []
    kwargs = _v1_kwargs(tmp_path, calls)
    run_presence_turn(**{**kwargs, "agent_factory": lambda **_kw: _sending_agent(calls, "Real answer", tmp_path, part="Retry part")})
    assert calls[0]["metadata"]["presence"]["previous_attempt"]["delivered_count"] is None  # the archived send
    pointer = _read_previous_turn(tmp_path, kwargs["event"].conversation_key)
    assert (pointer["transport_sends"], pointer["delivery"]) == (["Retry part"], "partly confirmed")


def test_a_rotation_during_the_turn_leaves_its_sends_unknown(tmp_path):
    from ouroboros.presence_runner import _read_previous_turn

    calls: list = []
    kwargs = _v1_kwargs(tmp_path, calls)
    run_presence_turn(**{**kwargs, "agent_factory": lambda **_kw: _sending_agent(
        calls, "Real answer", tmp_path, part="Mid part", rotate_first=True)})
    pointer = _read_previous_turn(tmp_path, kwargs["event"].conversation_key)
    assert (pointer["transport_sends"], pointer["delivery"]) == ([], "unknown")


def test_a_presence_placeholder_owes_no_terminal_projection_but_its_rerun_does(tmp_path, monkeypatch):
    """The target's terminal-projection sweep must not post the placeholder's failure into the room; the
    re-run's own completion originates the room's terminal row, even when a stale marker was inherited."""
    from ouroboros.terminal_projection import reconcile_terminal_projections

    from ouroboros.terminal_projection import SETTLEMENT_NONE, settle_terminal_projection

    task_id = _task_id(_admission(), _event())
    _reconciled(tmp_path, monkeypatch, task_id)
    assert reconcile_terminal_projections(tmp_path) == 0  # nothing owed for a placeholder
    chat = tmp_path / "logs" / "chat.jsonl"
    rows = [json.loads(line) for line in chat.read_text(encoding="utf-8").splitlines()] if chat.exists() else []
    assert not [row for row in rows if row.get("type") == "task_summary"]
    # An install on the previous release already recorded readiness for the placeholder (its chat append
    # failed): the direct settlement path, as startup recovery calls it, must not publish it either.
    write_task_result(tmp_path, task_id, STATUS_FAILED, canonical_terminal_projection_ready={
        "summary_id": f"task-terminal:{task_id}", "token": "stale", "attempt": {}, "task_done_ts": "2026-05-28T00:00:05+00:00",
        "chat_id": 7})
    assert settle_terminal_projection(tmp_path, task_id) == SETTLEMENT_NONE
    rows = [json.loads(line) for line in chat.read_text(encoding="utf-8").splitlines()] if chat.exists() else []
    assert not [row for row in rows if row.get("type") == "task_summary"]
    # An install that ran the sweep before this rule left a failed marker on the placeholder.
    write_task_result(tmp_path, task_id, STATUS_FAILED, canonical_terminal_projection={
        "summary_id": f"task-terminal:{task_id}", "summary_kind": "terminal_root_projection", "attempt": {}})
    calls: list = []
    first = run_presence_turn(admission=_admission(), event=_event(), repo_dir=tmp_path, drive_root=tmp_path,
                              agent_factory=lambda **_kw: _answering_agent(calls, "Real answer", tmp_path),
                              gate=PresenceTurnGate(1))
    stored = load_task_result(tmp_path, task_id)
    assert first.text == "Real answer" and stored["status"] == STATUS_COMPLETED
    assert {"canonical_terminal_projection", "canonical_terminal_projection_ready"} <= set(stored["superseded_placeholder"])
    assert reconcile_terminal_projections(tmp_path) == 1  # the re-run's completion owes and gets its row
    rows = [json.loads(line) for line in chat.read_text(encoding="utf-8").splitlines()]
    summaries = [row for row in rows if row.get("type") == "task_summary" and row.get("task_id") == task_id]
    assert [row["status"] for row in summaries] == ["completed"] and summaries[0]["presence_provenance"]["provider"] == "telegram"
    # An ordinary orphaned root (no presence identity) still gets its failed terminal row.
    _sweep(tmp_path, monkeypatch, "plain-orphan", metadata={"source": "chat"})
    assert reconcile_terminal_projections(tmp_path) == 1
    rows = [json.loads(line) for line in chat.read_text(encoding="utf-8").splitlines()]
    assert [row["status"] for row in rows if row.get("type") == "task_summary" and row.get("task_id") == "plain-orphan"] == ["failed"]
