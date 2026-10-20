"""Integration-secret HITL: showSecretHitl asks, the protected form's saveSecretToken saves.

Proved against a real task store, a real Settings file and the real decision ingress:
the value reaches ``settings.json`` under the skill's declared key and NOWHERE else
(response, store, mailbox, history, audit, logs), the model receives metadata-only
``secret_filled``/``secret_cancelled``, ordinary answer paths refuse the request, and
save/cancel/failure/duplicate/interruption keep ``saved`` truthful. The canaries have no
token shape, so no redaction pattern could be what keeps them out of an output.
"""

from __future__ import annotations

import json
import logging
import pathlib
import uuid
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros import owner_quiz
from ouroboros import secret_requests as sr
from ouroboros.contracts.skill_manifest import SkillManifestError, parse_skill_manifest_text
from ouroboros.task_results import write_task_result

CANARY = "zq7-CANARY-Ωlj3~p0+kx/2 mid space"
CANARY_B = "Second·canary·entry-9"
TASK = "task-1"

_MANIFEST = """---
name: {name}
description: Test integration.
version: {version}
type: script
runtime: python3
timeout_sec: 30
env_from_settings: [{env}]
secret_fields:
{fields}
scripts:
  - name: run.py
---
# {name}
"""


def _write_skill(root: pathlib.Path, name: str = "fakeplug", *, env: str = "FAKEPLUG_TOKEN, FAKEPLUG_REGION",
                 fields: str = "  botToken: FAKEPLUG_TOKEN", version: str = "0.1.0") -> pathlib.Path:
    skill = root / "skills" / "external" / name
    (skill / "scripts").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(_MANIFEST.format(name=name, env=env, fields=fields, version=version), encoding="utf-8")
    (skill / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
    return skill


@pytest.fixture
def env(tmp_path, monkeypatch, caplog):
    from ouroboros import config as cfg
    from ouroboros.gateway import secret_decision as sd
    from ouroboros.gateway import task_decision as td
    import supervisor.message_bus as mb
    import supervisor.queue as q

    root = tmp_path
    monkeypatch.setattr(cfg, "DATA_DIR", root)
    monkeypatch.setattr(cfg, "SETTINGS_PATH", root / "settings.json")
    monkeypatch.setattr(cfg, "get_skills_repo_path", lambda: "")
    cfg.reset_runtime_mode_baseline_for_tests()
    _write_skill(root)
    write_task_result(root, TASK, "running", chat_id=1)
    frames, events = [], []

    class Bridge:
        def send_quiz_state(self, quiz_id, task_id, state, **fields):
            frames.append({"quiz_id": quiz_id, "task_id": task_id, "state": state, **fields})

    monkeypatch.setattr(mb, "get_bridge", lambda: Bridge())
    monkeypatch.setattr(mb, "DATA_DIR", root)
    monkeypatch.setattr(mb, "publish_event", lambda *args: events.append(args))
    live = {"task": {"id": TASK}}
    monkeypatch.setattr(td, "_live_root_task", lambda task_id: (live["task"], "") if live["task"] else (None, "task_not_live"))
    monkeypatch.setattr(q, "_task_drive_for_task", lambda task, task_id: root, raising=False)
    app = Starlette(routes=[Route("/api/decisions/secret", endpoint=sd.api_secret_decision, methods=["POST"]),
                            Route("/api/decisions", endpoint=td.api_decision_answer, methods=["POST"])])
    app.state.drive_root = root
    caplog.set_level(logging.DEBUG)
    yield SimpleNamespace(root=root, client=TestClient(app), frames=frames, events=events, live=live, caplog=caplog)
    cfg.reset_runtime_mode_baseline_for_tests()


def _ask(env, fields=("botToken",), plugin="fakeplug") -> str:
    request = sr.resolve_secret_request(env.root, plugin, list(fields))
    quiz_id = uuid.uuid4().hex
    owner_quiz.record_asked(env.root, TASK, quiz_id=quiz_id, question=sr.question_text(request), options=[],
                            wait_for_answer=True, chat_id=1, secret_request=request)
    owner_quiz.record_publication(env.root, TASK, quiz_id, 1)
    return quiz_id


def _save(env, quiz_id, values, request_id="r1"):
    return env.client.post("/api/decisions/secret", json={
        "request_id": request_id, "decision_id": f"quiz:{TASK}:{quiz_id}", "values": values})


def _cancel(env, quiz_id, request_id="c1"):
    return env.client.post("/api/decisions/secret", json={
        "request_id": request_id, "decision_id": f"quiz:{TASK}:{quiz_id}", "action": "cancel"})


def _settings(env) -> dict:
    path = env.root / "settings.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _mail(env):
    from ouroboros.owner_mailbox import drain_owner_entries

    return drain_owner_entries(env.root, TASK, set())


def _block(env, quiz_id):
    return owner_quiz.quiz_states(env.root, TASK)[quiz_id]


def _assert_absent(env, *values, texts=()):
    """No file of the data root but Settings, no log record and no given text holds a value."""
    needles = {form for value in values for form in (
        value.encode(), json.dumps(value)[1:-1].encode(), json.dumps(value, ensure_ascii=False)[1:-1].encode())}
    for path in env.root.rglob("*"):
        if path.is_file() and not path.name.startswith("settings.json"):
            data = path.read_bytes()
            for needle in needles:
                assert needle not in data, f"a submitted value leaked into {path}"
    logged = env.caplog.text.encode()
    for text in [*texts, logged.decode()]:
        for value in values:
            assert value not in text and json.dumps(value)[1:-1] not in text


# ------------------------------------------------------------------ manifest

def test_canonical_manifest_field_maps_ids_to_declared_env_keys():
    manifest = parse_skill_manifest_text(_MANIFEST.format(
        name="x", version="1", env="X_TOKEN, X_REGION", fields="  botToken: X_TOKEN"))
    assert manifest.secret_fields == {"botToken": "X_TOKEN"}
    assert "secret_fields" not in manifest.raw_extra  # a canonical field, not a forward-compat extra
    for fields, problem in (("  botToken: X_OTHER", "env_from_settings"), ("  9bad: X_TOKEN", "letter"),
                            ("  a: X_TOKEN\n  b: X_TOKEN", "two fields")):
        with pytest.raises(SkillManifestError, match=problem):
            parse_skill_manifest_text(_MANIFEST.format(name="x", version="1", env="X_TOKEN", fields=fields))


def test_telegram_declares_its_bot_token_as_a_custom_secret_field():
    repo = pathlib.Path(__file__).resolve().parents[1]
    manifest = parse_skill_manifest_text((repo / "skills" / "telegram" / "SKILL.md").read_text(encoding="utf-8"))
    assert manifest.secret_fields == {"botToken": "TELEGRAM_BOT_TOKEN"}
    assert sr.declared_secret_fields(manifest) == {"botToken": "TELEGRAM_BOT_TOKEN"}


@pytest.mark.parametrize("key", ["OPENROUTER_API_KEY", "OUROBOROS_NETWORK_PASSWORD", "GITHUB_TOKEN", "TOTAL_BUDGET"])
def test_a_plugin_name_cannot_front_a_core_or_provider_setting(env, key):
    _write_skill(env.root, "fronted", env=key, fields=f"  apiKey: {key}")
    with pytest.raises(sr.SecretRequestError, match="custom Settings secret") as caught:
        sr.resolve_secret_request(env.root, "fronted", ["apiKey"])
    assert caught.value.code == "SECRET_FIELDS_INVALID"


def test_requests_name_only_declared_fields_of_an_installed_skill(env):
    for plugin, fields, code in (("missing", ["botToken"], "SECRET_PLUGIN_UNKNOWN"),
                                 ("fakeplug", ["region"], "SECRET_FIELD_UNDECLARED"),
                                 ("fakeplug", ["botToken", "botToken"], "SECRET_FIELDS_INVALID"),
                                 ("fakeplug", [], "SECRET_FIELDS_INVALID")):
        with pytest.raises(sr.SecretRequestError) as caught:
            sr.resolve_secret_request(env.root, plugin, fields)
        assert caught.value.code == code
    request = sr.resolve_secret_request(env.root, "fakeplug", ["botToken"])
    assert request["fields"] == [{"id": "botToken", "setting_key": "FAKEPLUG_TOKEN"}]
    assert request["content_hash"]


# ------------------------------------------------------------------ the tool

def test_show_secret_hitl_records_a_waiting_metadata_only_question(env, monkeypatch):
    from ouroboros.tools import core_artifacts, owner_delivery
    from ouroboros.tools.registry import ToolContext
    from ouroboros.tools.secret_hitl import _show_secret_hitl, get_tools

    sent = []
    monkeypatch.setattr(owner_delivery, "deliver_owner_event", lambda ctx, evt: sent.append(evt) or "live")
    monkeypatch.setattr(core_artifacts, "_quiz_host_facts", lambda *args: "Asked by task task-1.")
    ctx = ToolContext(repo_dir=env.root, drive_root=env.root)
    ctx.task_id, ctx.current_chat_id, ctx.owner_wait_callback = TASK, 1, lambda *args: "answer"
    [entry] = get_tools()
    assert entry.name == "showSecretHitl" and entry.safety_policy == "skip"
    assert entry.schema["parameters"]["required"] == ["pluginName", "fields"]

    text = _show_secret_hitl(ctx, pluginName="fakeplug", fields=["botToken"])
    assert text.startswith("OK: secret request ") and "secret_filled {pluginName, fields}" in text
    [event] = sent
    assert event["type"] == "send_quiz" and event["wait_for_answer"] is True and event["options"] == []
    block = _block(env, event["quiz_id"])
    assert block["secret_request"]["plugin_name"] == "fakeplug"
    assert block["secret_request"]["fields"] == [{"id": "botToken", "setting_key": "FAKEPLUG_TOKEN"}]
    assert block["wait_for_answer"] is True and ctx._owner_wait_requested == event["quiz_id"]

    child = ToolContext(repo_dir=env.root, drive_root=env.root, task_metadata={"parent_task_id": "root"})
    child.task_id, child.owner_wait_callback = "child", lambda *args: ""
    assert "CAPABILITY_UNAVAILABLE" in _show_secret_hitl(child, pluginName="fakeplug", fields=["botToken"])
    refused = _show_secret_hitl(ctx, pluginName="fakeplug", fields=["FAKEPLUG_TOKEN"])
    assert "SECRET_FIELD_UNDECLARED" in refused and len(sent) == 1


# ------------------------------------------------------------- save / cancel

def test_save_writes_settings_only_and_delivers_metadata_only_secret_filled(env):
    quiz_id = _ask(env)
    response = _save(env, quiz_id, {"botToken": CANARY})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["saved"] is True and body["state"] == "answered" and body["duplicate"] is False
    assert body["secret_filled"] == {"pluginName": "fakeplug", "fields": ["botToken"]}
    assert body["grant_missing"] == ["FAKEPLUG_TOKEN"]  # saving granted nothing
    assert response.headers["cache-control"] == "no-store"
    assert _settings(env)["FAKEPLUG_TOKEN"] == CANARY

    [mail] = [entry for entry in _mail(env) if entry.get("kind") == "quiz_answer"]
    assert 'secret_filled: {"pluginName": "fakeplug", "fields": ["botToken"]}' in mail["text"]
    assert "Secret filled: fakeplug, botToken" in mail["text"]
    assert "Saved is not connected" in mail["text"] and "Verbatim" not in mail["text"]
    block = _block(env, quiz_id)
    assert block["secret_outcome"]["event"] == "secret_filled" and "comment" not in block
    assert block["secret_save"]["state"] == "saved"
    assert owner_quiz.late_answer_owner_text(block) == ""  # never speakable as the owner's words
    from tests import _dialogue

    history = [row for row in _dialogue.chat_rows(env.root) if row.get("type") == "quiz_answer"]
    assert history and "Secret filled: fakeplug, botToken" in history[-1]["text"]
    assert any(frame.get("secret_outcome", {}).get("event") == "secret_filled" for frame in env.frames)
    audit = [json.loads(line) for line in (env.root / "logs" / "events.jsonl").read_text().splitlines()]
    assert any(row.get("action") == "secret_request_save" and row.get("fields") == ["botToken"] for row in audit)
    _assert_absent(env, CANARY, texts=[response.text, json.dumps(env.frames, ensure_ascii=False)])

    replay = _save(env, quiz_id, {"botToken": CANARY})
    assert replay.status_code == 200 and replay.json()["duplicate"] is True
    assert len([entry for entry in _mail(env) if entry.get("kind") == "quiz_answer"]) == 1
    loser = _save(env, quiz_id, {"botToken": CANARY_B}, request_id="other-tab")
    assert loser.status_code == 409 and loser.json()["saved"] is False
    assert loser.json()["reason_code"] == "secret_request_settled" and loser.json()["secret_filled"]["fields"] == ["botToken"]
    assert _settings(env)["FAKEPLUG_TOKEN"] == CANARY
    _assert_absent(env, CANARY, CANARY_B, texts=[replay.text, loser.text])


def test_cancel_saves_nothing_and_wakes_the_task_with_a_distinct_cancellation(env):
    quiz_id = _ask(env)
    response = _cancel(env, quiz_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "closed" and body["saved"] is False
    assert body["secret_cancelled"] == {"pluginName": "fakeplug", "fields": ["botToken"]}
    [mail] = [entry for entry in _mail(env) if entry.get("kind") == "quiz_closed"]
    assert mail["text"].startswith("[Owner secret request cancelled]")
    assert 'secret_cancelled: {"pluginName": "fakeplug", "fields": ["botToken"]}' in mail["text"]
    assert "[Host note] The task can tell the owner what is still missing" in mail["text"]
    assert "unknown" not in mail["text"]
    late = _save(env, quiz_id, {"botToken": CANARY})
    assert late.status_code == 409 and late.json()["reason_code"] == "secret_request_settled"
    assert "FAKEPLUG_TOKEN" not in _settings(env)
    assert _cancel(env, quiz_id).json()["duplicate"] is True
    _assert_absent(env, CANARY, texts=[late.text])


def test_a_save_first_wins_over_a_later_cancel(env):
    quiz_id = _ask(env)
    assert _save(env, quiz_id, {"botToken": CANARY}).status_code == 200
    cancel = _cancel(env, quiz_id)
    assert cancel.status_code == 409 and cancel.json()["state"] == "answered"
    assert cancel.json()["secret_filled"]["pluginName"] == "fakeplug"
    assert _settings(env)["FAKEPLUG_TOKEN"] == CANARY


def test_ordinary_answer_paths_refuse_options_and_comments_without_echo(env):
    import asyncio

    from ouroboros.gateway.task_decision import answer_decision

    quiz_id = _ask(env)
    decision = f"quiz:{TASK}:{quiz_id}"
    for body in ({"request_id": "a1", "decision_id": decision, "comment": CANARY_B},
                 {"request_id": "a2", "decision_id": decision, "option_index": 0, "comment": CANARY_B}):
        response = env.client.post("/api/decisions", json=body)
        assert response.status_code == 409 and response.json()["reason_code"] == "secret_request_form_only"
        assert CANARY_B not in response.text
    # The host-service relay (Telegram's /chat/decision) reaches the same ingress.
    status, payload = asyncio.run(answer_decision(env.root, {"request_id": "a3", "decision_id": decision,
                                                             "comment": CANARY_B}, source="telegram"))
    assert status == 409 and payload["reason_code"] == "secret_request_form_only"
    direct = owner_quiz.record_answered(env.root, TASK, quiz_id=quiz_id, option_index=None,
                                        request_id="a4", comment=CANARY_B)
    assert direct["ok"] is False and direct["error"] == "secret_request_form_only"
    block = _block(env, quiz_id)
    assert block["state"] == "open" and "comment" not in block and "secret_outcome" not in block
    assert not _mail(env)
    # The owner's generic close of the card is its cancel.
    close = env.client.post("/api/decisions", json={"request_id": "x1", "decision_id": decision, "action": "close"})
    assert close.status_code == 200 and close.json()["secret_cancelled"]["fields"] == ["botToken"]
    _assert_absent(env, CANARY_B, texts=[json.dumps(payload, ensure_ascii=False)])


def test_values_must_match_the_request_and_be_keepable(env):
    quiz_id = _ask(env)
    cases = ({"botToken": "abcdefgh..."}, {"botToken": "***set***"}, {"botToken": "  "},
             {"botToken": "line\nbreak"}, {"botToken": CANARY, CANARY_B: "x"}, {})
    for values in cases:
        response = _save(env, quiz_id, values)
        assert response.status_code == 400 and response.json()["saved"] is False
        assert CANARY_B not in response.text and CANARY not in response.text
    assert [_save(env, quiz_id, values).json()["reason_code"] for values in cases[:2]] == [
        "secret_value_placeholder", "secret_value_placeholder"]
    assert not _settings(env) and _block(env, quiz_id)["state"] == "open"
    assert "secret_save" not in _block(env, quiz_id)
    _assert_absent(env, CANARY, CANARY_B)


def test_stale_skill_and_finished_task_write_nothing(env):
    stale = _ask(env)
    _write_skill(env.root, version="0.2.0")  # the reviewed content changed under the pending request
    response = _save(env, stale, {"botToken": CANARY})
    assert response.status_code == 409 and response.json()["reason_code"] == "secret_request_stale"
    assert "secret_save" not in _block(env, stale)
    finished = _ask(env)
    owner_quiz.reconcile_terminal(env.root, TASK)
    response = _save(env, finished, {"botToken": CANARY})
    assert response.status_code == 409 and response.json()["reason_code"] == "secret_request_inactive"
    assert not _settings(env)
    _assert_absent(env, CANARY)


def test_a_finished_asker_writes_nothing_even_before_its_terminal_reconcile(env):
    """The task-done seam has not expired the block yet (crash or post-work window): the save
    reads the live queue, heals the question like an ordinary answer, and writes nothing."""
    quiz_id = _ask(env)
    env.live["task"] = None
    assert _block(env, quiz_id)["state"] == "open"
    response = _save(env, quiz_id, {"botToken": CANARY})
    assert response.status_code == 409 and response.json()["reason_code"] == "secret_request_inactive"
    assert response.json()["saved"] is False and not _settings(env)
    assert _block(env, quiz_id)["state"] == "expired_terminal" and "secret_save" not in _block(env, quiz_id)
    _assert_absent(env, CANARY)


def test_a_refused_settings_write_saves_nothing_and_stays_retryable(env, monkeypatch):
    from ouroboros.gateway import owner_settings

    quiz_id = _ask(env)
    real_update, calls = owner_settings._owner_update_settings, []

    def locked_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise owner_settings.SettingsLockUnavailable("another process holds the settings lock")
        return real_update(*args, **kwargs)

    monkeypatch.setattr(owner_settings, "_owner_update_settings", locked_once)
    refused = _save(env, quiz_id, {"botToken": CANARY})
    assert refused.status_code == 503 and refused.json() == {
        "ok": False, "saved": False, "reason_code": "settings_locked", "decision_id": f"quiz:{TASK}:{quiz_id}",
        "error": "Settings could not be written (SettingsLockUnavailable); nothing was saved. Try again."}
    block = _block(env, quiz_id)
    assert block["state"] == "open" and "secret_save" not in block and "secret_outcome" not in block
    assert not _mail(env) and not any(frame.get("secret_outcome") for frame in env.frames)
    retry = _save(env, quiz_id, {"botToken": CANARY})
    assert retry.status_code == 200 and retry.json()["saved"] is True
    _assert_absent(env, CANARY, texts=[refused.text])


def test_interruption_after_the_settings_commit_stays_saved_and_unknown_never_reposted(env, monkeypatch):
    quiz_id = _ask(env)
    real_finalize = sr.finalize_save

    def crash(*_args, **_kwargs):
        raise RuntimeError("store unavailable after the settings commit")

    monkeypatch.setattr(sr, "finalize_save", crash)
    response = _save(env, quiz_id, {"botToken": CANARY})
    assert response.status_code == 500
    body = response.json()
    assert body["saved"] is True and body["reason_code"] == "secret_completion_unrecorded"
    assert _settings(env)["FAKEPLUG_TOKEN"] == CANARY
    block = _block(env, quiz_id)
    # The durable pre-write marker survives the process: unknown, never reported filled.
    assert block["state"] == "open" and block["secret_save"]["state"] == "writing"
    assert block["secret_save"]["request_id"] == "r1" and "secret_outcome" not in block
    assert not _mail(env)
    monkeypatch.setattr(sr, "finalize_save", real_finalize)
    # Another process may not close it underneath a claimed write.
    model = owner_quiz.record_closed(env.root, TASK, quiz_id=quiz_id, reason="done", actor_task_id=TASK)
    assert model["ok"] is False and model["error"] == "secret_save_in_progress"
    # Status first: the same request is never repeated over a write that may have landed.
    repeat = _save(env, quiz_id, {"botToken": CANARY})
    assert repeat.status_code == 409 and repeat.json()["saved"] is None
    assert repeat.json()["reason_code"] == "secret_save_unconfirmed" and repeat.json()["save_unconfirmed"] is True
    assert not _mail(env)
    # The owner's explicit save-again is a new request; it replaces the unknown and completes once.
    again = _save(env, quiz_id, {"botToken": CANARY}, request_id="r2")
    assert again.status_code == 200 and again.json()["saved"] is True
    assert _block(env, quiz_id)["secret_save"]["interrupted_request_id"] == "r1"
    assert len([entry for entry in _mail(env) if entry.get("kind") == "quiz_answer"]) == 1
    _assert_absent(env, CANARY, texts=[response.text, repeat.text, again.text])


def test_an_unreadable_read_back_is_written_but_never_reported_filled(env, monkeypatch):
    from ouroboros.gateway import owner_settings

    quiz_id = _ask(env)

    def unreadable():
        raise OSError("settings read failed")

    monkeypatch.setattr(owner_settings, "_owner_settings_document", unreadable)
    response = _save(env, quiz_id, {"botToken": CANARY})
    assert response.status_code == 500
    assert response.json()["saved"] is True and response.json()["reason_code"] == "secret_save_unverified"
    block = _block(env, quiz_id)
    assert block["state"] == "open" and block["secret_save"]["state"] == "writing"
    assert "secret_outcome" not in block and not _mail(env)
    _assert_absent(env, CANARY, texts=[response.text])


def test_an_interrupted_claim_is_taken_over_or_cancelled_with_its_unknown_disclosed(env):
    taken = _ask(env)
    assert sr.claim_save(env.root, TASK, taken, "dead-process")["ok"] is True  # then the process died
    response = _save(env, taken, {"botToken": CANARY}, request_id="new")
    assert response.status_code == 200
    assert _block(env, taken)["secret_save"]["interrupted_request_id"] == "dead-process"
    cancelled = _ask(env)
    sr.claim_save(env.root, TASK, cancelled, "dead-process")
    body = _cancel(env, cancelled).json()
    # The cancel saved nothing itself, but cannot say nothing is stored: saved is unknown.
    assert body["state"] == "closed" and body["interrupted_save"] is True and body["saved"] is None
    assert "unknown" in body["closed_reason"] and "nothing was saved" not in body["closed_reason"]
    [mail] = [entry for entry in _mail(env) if entry.get("kind") == "quiz_closed"]
    assert "whether its value reached Settings (FAKEPLUG_TOKEN) is unknown" in mail["text"]
    assert "nothing was saved" not in mail["text"] and "unchanged" not in mail["text"]


def test_recovery_redelivers_a_filled_secret_through_its_own_path(env, monkeypatch):
    import asyncio

    from ouroboros import owner_mailbox
    from ouroboros.gateway.task_decision import recover_question_deliveries

    quiz_id = _ask(env)
    real_write = owner_mailbox.write_owner_message
    monkeypatch.setattr(owner_mailbox, "write_owner_message", lambda *args, **kwargs: False)
    assert _save(env, quiz_id, {"botToken": CANARY}).json()["saved"] is True
    assert _block(env, quiz_id)["delivery"]["reason_code"] == "mailbox_write_failed"
    monkeypatch.setattr(owner_mailbox, "write_owner_message", real_write)
    row = owner_quiz.quiz_states(env.root, TASK)
    row[quiz_id]["delivery"]["next_retry_at"] = "2000-01-01T00:00:00+00:00"
    write_task_result(env.root, TASK, "running", owner_quiz=row)
    asyncio.run(recover_question_deliveries(env.root))
    [mail] = [entry for entry in _mail(env) if entry.get("kind") == "quiz_answer"]
    assert "Secret filled: fakeplug, botToken" in mail["text"]
    from ouroboros.owner_quiz import SECRET_OUTCOME_ROW_KIND

    ctx, messages = _drain(env)  # the recovered delivery is the same typed host row
    assert SECRET_OUTCOME_ROW_KIND in _kinds(messages) and ctx._owner_directives == []
    _assert_absent(env, CANARY)


def test_secret_requests_stay_off_the_extension_bus_but_reach_the_web(env):
    import supervisor.message_bus as mb

    quiz_id = uuid.uuid4().hex
    request = sr.resolve_secret_request(env.root, "fakeplug", ["botToken"])
    owner_quiz.record_asked(env.root, TASK, quiz_id=quiz_id, question=sr.question_text(request), options=[],
                            wait_for_answer=True, chat_id=1, secret_request=request)
    bridge = mb.LocalChatBridge({})
    shown = []
    bridge._broadcast_fn = shown.append
    ok, _ = bridge.send_quiz(1, quiz_id=quiz_id, question=sr.question_text(request), options=[], task_id=TASK,
                             wait_for_answer=True)
    assert ok
    [frame] = [frame for frame in shown if frame.get("type") == "quiz"]
    assert frame["secret_request"]["plugin_name"] == "fakeplug"
    bridge.send_quiz_state(quiz_id, TASK, "open", wait_for_answer=False)
    assert not [event for event in env.events if event[0] in ("chat.quiz", "chat.quiz_state")]
    from ouroboros.gateway.history import _assemble_history_response

    replay = json.loads(_assemble_history_response(env.root, 1, 10, 0))["messages"]
    assert any(row.get("quiz", {}).get("secret_request", {}).get("plugin_name") == "fakeplug" for row in replay)


def test_the_leak_scan_is_not_vacuous(env):
    """The absence assertions above prove something only if the scan finds an escaped echo."""
    probe = env.root / "logs" / "probe.jsonl"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(json.dumps({"echo": CANARY}) + "\n", encoding="utf-8")  # ASCII-escaped form
    with pytest.raises(AssertionError, match="leaked into"):
        _assert_absent(env, CANARY)
    probe.unlink()
    with pytest.raises(AssertionError):
        _assert_absent(env, CANARY, texts=[f"error: {CANARY}"])


def test_simultaneous_saves_and_cancel_have_exactly_one_winner(env):
    import threading

    from ouroboros.gateway.secret_decision import save_secret

    quiz_id = _ask(env)
    decision = f"quiz:{TASK}:{quiz_id}"
    bodies = [{"request_id": "tab-a", "decision_id": decision, "values": {"botToken": CANARY}},
              {"request_id": "tab-b", "decision_id": decision, "values": {"botToken": CANARY_B}},
              {"request_id": "tab-c", "decision_id": decision, "action": "cancel"}]
    results, gate = {}, threading.Barrier(len(bodies))

    def run(body):
        gate.wait()
        results[body["request_id"]] = save_secret(env.root, body)

    threads = [threading.Thread(target=run, args=(body,)) for body in bodies]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    winners = [rid for rid, (status, _payload) in results.items() if status == 200]
    assert len(winners) == 1, results
    block = _block(env, quiz_id)
    if winners == ["tab-c"]:
        assert block["state"] == "closed" and "FAKEPLUG_TOKEN" not in _settings(env)
    else:
        assert block["state"] == "answered" and block["request_id"] == winners[0]
        assert _settings(env)["FAKEPLUG_TOKEN"] == {"tab-a": CANARY, "tab-b": CANARY_B}[winners[0]]
    assert all(status == 409 for rid, (status, _payload) in results.items() if rid not in winners)
    _assert_absent(env, CANARY, CANARY_B, texts=[json.dumps(results, ensure_ascii=False)])


def test_a_task_ending_mid_save_keeps_the_outcome_without_inventing_an_owner_turn(env, monkeypatch):
    from ouroboros.gateway import owner_settings
    import supervisor.message_bus as mb

    quiz_id = _ask(env)
    real_update = owner_settings._owner_update_settings

    def ends_then_writes(*args, **kwargs):
        owner_quiz.reconcile_terminal(env.root, TASK)  # the asking task finishes during the write
        env.live["task"] = None
        return real_update(*args, **kwargs)

    monkeypatch.setattr(owner_settings, "_owner_update_settings", ends_then_writes)
    spoken = []
    monkeypatch.setattr(mb, "accept_local_message", lambda *args, **kwargs: spoken.append(args) or ({}, False))
    response = _save(env, quiz_id, {"botToken": CANARY})
    assert response.status_code == 200 and response.json()["saved"] is True
    block = _block(env, quiz_id)
    assert block["state"] == "answered" and block["answered_after_terminal"] is True
    assert block["delivery"]["state"] == "delivered" and block["delivery"]["history_at"]
    assert not spoken and not _mail(env)  # the history row is the record; no owner words were made up
    from ouroboros.dialogue_provenance import row_author
    from tests import _dialogue

    [row] = [row for row in _dialogue.chat_rows(env.root) if row.get("type") == "quiz_answer"]
    assert row_author(row)["kind"] == "host"  # the late record is the host's, never the owner's
    _assert_absent(env, CANARY)


def test_the_waiting_task_wakes_with_metadata_only_secret_filled_in_its_next_model_input(env, monkeypatch):
    """Public tool -> real publication -> the loop's own wait -> the owner's direct POST during it
    -> the same task's next model input carries typed ``secret_filled`` and never the value."""
    import queue

    import supervisor.message_bus as mb
    from ouroboros.loop_round_limits import _drain_incoming_messages
    from ouroboros.owner_wait import wait_after_tools
    from ouroboros.task_results import load_task_result
    from ouroboros.tools import core_artifacts
    from ouroboros.tools.secret_hitl import _show_secret_hitl
    from supervisor.events_chat_delivery import _handle_send_quiz
    from tests.test_native_owner_wait import native_context

    ctx = native_context(env.root)
    ctx.current_chat_id, ctx.task_metadata, ctx._owner_wait_requested = 1, {}, ""
    monkeypatch.setattr(core_artifacts, "_quiz_host_facts", lambda *args: "Asked by task root-1.")
    result = _show_secret_hitl(ctx, pluginName="fakeplug", fields=["botToken"])
    assert result.startswith("OK: secret request "), result
    event = ctx.event_queue.get_nowait()
    shown = []
    bridge = mb.LocalChatBridge({})
    bridge._broadcast_fn = shown.append
    _handle_send_quiz(event, SimpleNamespace(DRIVE_ROOT=env.root, bridge=bridge))
    assert any(frame.get("type") == "quiz" and frame.get("secret_request") for frame in shown)
    quiz_id = event["quiz_id"]
    posted = []

    def owner_fills_the_form(_seconds):
        if not posted:
            posted.append(env.client.post("/api/decisions/secret", json={
                "request_id": "form-1", "decision_id": f"quiz:{ctx.task_id}:{quiz_id}",
                "values": {"botToken": CANARY}}))

    monkeypatch.setattr("ouroboros.owner_wait.time.sleep", owner_fills_the_form)
    messages = [{"role": "tool", "tool_call_id": "c1", "content": result}]
    wait_after_tools(ctx, messages, {}, {}, 1, [], set())
    assert posted and posted[0].status_code == 200 and posted[0].json()["saved"] is True
    wait = load_task_result(env.root, ctx.task_id)["owner_wait"]
    assert wait["state"] == "resumed" and wait["resume_reason"] == "answer" and wait["quiz_id"] == quiz_id
    assert ctx._owner_wait_requested == ""

    directives, before = list(ctx._owner_directives), len(messages)
    _drain_incoming_messages(messages, queue.Queue(), env.root, ctx.task_id, None, ctx._loop_mailbox_seen_ids,
                             owner_ctx=ctx)
    model_input = "\n".join(str(row.get("content") or "") for row in messages[before:])
    assert 'secret_filled: {"pluginName": "fakeplug", "fields": ["botToken"]}' in model_input
    assert "Secret filled: fakeplug, botToken" in model_input and "do not ask for it again" in model_input
    assert ctx._owner_directives == directives  # no owner words were made up for the host's outcome
    from ouroboros.owner_quiz import SECRET_OUTCOME_ROW_KIND

    assert SECRET_OUTCOME_ROW_KIND in _kinds(messages[before:]) and "owner_words" not in _kinds(messages[before:])
    assert _settings(env)["FAKEPLUG_TOKEN"] == CANARY
    _assert_absent(env, CANARY, texts=[json.dumps(messages, ensure_ascii=False), posted[0].text,
                                       json.dumps(shown, ensure_ascii=False)])


# ------------------------------------------------------- typed host provenance

_BASE_MANIFEST_FIELDS = [
    "name", "description", "version", "type", "when_to_use", "requires", "os", "runtime", "timeout_sec",
    "env_from_settings", "scripts", "entry", "permissions", "conflicts", "subscribe_events", "companion_processes",
    "scheduled_tasks", "ui_tab", "contributes", "plugin_api", "model_experience", "context_snapshot", "body", "raw_extra",
    "schema_version",
]


def test_secret_fields_is_appended_so_positional_manifest_construction_keeps_every_slot():
    import dataclasses

    from ouroboros.contracts.skill_manifest import SkillManifest

    assert [item.name for item in dataclasses.fields(SkillManifest)] == [*_BASE_MANIFEST_FIELDS, "secret_fields"]
    positional = SkillManifest("n", "d", "1", "script", "", [], "any", "python3", 30, ["K"], [{"name": "run.py"}])
    assert positional.env_from_settings == ["K"] and positional.scripts == [{"name": "run.py"}]
    assert positional.secret_fields == {}


def _drain(env):
    import queue

    from ouroboros.loop_round_limits import _drain_incoming_messages

    ctx = SimpleNamespace(task_id=TASK, task_metadata={}, drive_root=env.root, budget_drive_root=env.root,
                          _owner_directives=[])
    messages = []
    _drain_incoming_messages(messages, queue.Queue(), env.root, TASK, None, set(), owner_ctx=ctx)
    return ctx, messages


def _kinds(messages):
    from ouroboros.context_budget import HOST_CONTEXT_KIND_KEY

    return [row.get(HOST_CONTEXT_KIND_KEY) for row in messages]


def test_a_filled_secret_is_a_typed_host_row_and_adds_nothing_to_the_owner_corpus(env):
    from ouroboros.context_budget import OWNER_WORDS_KIND
    from ouroboros.owner_quiz import SECRET_OUTCOME_ROW_KIND

    quiz_id = _ask(env)
    assert _save(env, quiz_id, {"botToken": CANARY}).status_code == 200
    ctx, messages = _drain(env)
    secret_rows = [row for row in messages if "secret_filled:" in str(row.get("content"))]
    assert len(secret_rows) == 1 and _kinds(secret_rows) == [SECRET_OUTCOME_ROW_KIND]
    assert OWNER_WORDS_KIND not in _kinds(messages)
    assert ctx._owner_directives == []  # the owner typed only the value; no owner words exist
    _assert_absent(env, CANARY, texts=[json.dumps(messages, ensure_ascii=False)])


@pytest.mark.parametrize("interrupted", [False, True])
def test_a_cancelled_secret_frame_is_host_typed_and_only_the_owner_act_is_retained(env, interrupted):
    from ouroboros.context_budget import OWNER_WORDS_KIND
    from ouroboros.owner_quiz import SECRET_OUTCOME_ROW_KIND

    quiz_id = _ask(env)
    if interrupted:
        sr.claim_save(env.root, TASK, quiz_id, "dead-process")
    assert _cancel(env, quiz_id).status_code == 200
    [mail] = [entry for entry in _mail(env) if entry.get("kind") == "quiz_closed"]
    assert mail["quiz_delivery"]["answer_snapshot"]["secret_outcome"]["event"] == "secret_cancelled"
    ctx, messages = _drain(env)
    frames = [row for row in messages if "[Owner secret request cancelled]" in str(row.get("content"))]
    assert len(frames) == 1 and _kinds(frames) == [SECRET_OUTCOME_ROW_KIND]
    assert OWNER_WORDS_KIND not in _kinds(messages)
    assert ctx._owner_directives == []  # a click is retained as an action, not invented owner wording
    assert _block(env, quiz_id)["closed_actor"] == "owner"
    assert _block(env, quiz_id)["secret_outcome"]["event"] == "secret_cancelled"
    assert "[Host note]" not in json.dumps(ctx._owner_directives)


def test_owner_corpus_readers_keep_secret_frames_host_authored(env):
    from ouroboros.dialogue_evidence import _row_projection
    from ouroboros.dialogue_provenance import row_author
    from ouroboros.owner_continue import owner_corpus_rows, owner_sources
    from ouroboros.owner_relay import _owner_row
    from ouroboros.task_results import load_task_result
    from ouroboros.tools.plan_dialogue import _quiz_text
    from tests import _dialogue

    filled, cancelled = _ask(env), _ask(env)
    assert _save(env, filled, {"botToken": CANARY}).status_code == 200
    assert _cancel(env, cancelled).status_code == 200
    rows = {(row.get("type"), (row.get("quiz") or {}).get("quiz_id")): row for row in _dialogue.chat_rows(env.root)}
    fill_row, cancel_row = rows[("quiz_answer", filled)], rows[("quiz_closed", cancelled)]
    assert row_author(fill_row)["kind"] == "host" and row_author(fill_row)["via"] == "secret_request"
    assert row_author(cancel_row)["kind"] == "host" and row_author(cancel_row)["via"] == "secret_request"
    assert cancel_row["quiz"]["closed_actor"] == "owner"
    assert not _owner_row(fill_row)  # never a quotable owner row
    assert _owner_row({**fill_row, "quiz": {k: v for k, v in fill_row["quiz"].items() if not k.startswith("secret_")}})
    assert _quiz_text(fill_row) == f"[secret request {filled}] secret_filled: fakeplug, botToken"
    for i, entry in enumerate(_mail(env), 1):
        assert _row_projection({**entry, "task_id": TASK}, "mailbox", i)["author"] == "host"
    later = owner_sources(env.root, load_task_result(env.root, TASK), TASK)["later"]
    assert later == [] and owner_corpus_rows({"later": later}) == []
    _assert_absent(env, CANARY, texts=[json.dumps(later, ensure_ascii=False)])


@pytest.mark.parametrize("change", ["mapping", "content", "unchanged"])
def test_skill_freshness_is_checked_after_waiting_for_the_real_settings_lock(env, monkeypatch, change):
    """A blocked POST must not use the mapping it saw before another writer's lock."""
    from concurrent.futures import ThreadPoolExecutor
    import threading

    from ouroboros import config as cfg

    cfg.SETTINGS_PATH.write_text('{"UNRELATED": "keep"}\n', encoding="utf-8")
    before = cfg.SETTINGS_PATH.read_bytes()
    quiz_id = _ask(env)
    acquire = cfg._acquire_settings_lock
    held = acquire()
    assert held is not None
    entered = threading.Event()

    def observed_acquire(*args, **kwargs):
        entered.set()
        return acquire(*args, **kwargs)

    monkeypatch.setattr(cfg, "_acquire_settings_lock", observed_acquire)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_save, env, quiz_id, {"botToken": CANARY}, "waited-save")
        try:
            assert entered.wait(2) and not pending.done()
            if change == "mapping":
                _write_skill(env.root, env="FAKEPLUG_TOKEN, FAKEPLUG_OTHER_TOKEN",
                             fields="  botToken: FAKEPLUG_OTHER_TOKEN", version="0.2.0")
            elif change == "content":
                _write_skill(env.root, version="0.2.0")
        finally:
            cfg._release_settings_lock(held)
        response = pending.result(timeout=5)
    body = response.json()
    if change == "unchanged":
        assert response.status_code == 200 and body["saved"] is True
        assert _settings(env)["FAKEPLUG_TOKEN"] == CANARY
    else:
        assert response.status_code == 409 and body["reason_code"] == "secret_request_stale", body
        assert body["saved"] is False and "secret_filled" not in body
        assert cfg.SETTINGS_PATH.read_bytes() == before
        block = _block(env, quiz_id)
        assert block["state"] == "open" and not block.get("secret_save")
        assert not _mail(env) and not env.frames
    _assert_absent(env, CANARY, texts=[response.text])


def test_a_paused_asker_keeps_filled_outcome_owed_until_it_reads_again(env, monkeypatch):
    """History records the save, but Pause must not discharge the asker's unread outcome."""
    import asyncio
    from ouroboros.gateway import task_decision as td

    quiz_id = _ask(env)
    live_reader = td._live_root_task
    monkeypatch.setattr(td, "_live_root_task", lambda task_id: (None, "task_not_reading"))
    response = _save(env, quiz_id, {"botToken": CANARY})
    assert response.status_code == 200 and response.json()["saved"] is True
    block = _block(env, quiz_id)
    assert block["delivery"]["history_at"]
    assert block["delivery"]["state"] == "owed"
    assert not block.get("discharged_at") and not _mail(env)

    monkeypatch.setattr(td, "_live_root_task", live_reader)
    quizzes = owner_quiz.quiz_states(env.root, TASK)
    quizzes[quiz_id]["delivery"]["next_retry_at"] = "2000-01-01T00:00:00+00:00"
    write_task_result(env.root, TASK, "running", owner_quiz=quizzes)
    asyncio.run(td.recover_question_deliveries(env.root))
    [mail] = [row for row in _mail(env) if row.get("kind") == "quiz_answer"]
    assert "Secret filled: fakeplug, botToken" in mail["text"]
    assert _block(env, quiz_id)["delivery"]["recipient_task_id"] == TASK
    _assert_absent(env, CANARY, texts=[response.text, mail["text"]])
