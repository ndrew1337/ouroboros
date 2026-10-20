"""Offline actor-consumer checks. Every memory, grant and dollar here is synthetic."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ouroboros.contracts.realtime import RealtimeSetupError
from ouroboros.realtime_turn import RealtimeTurn
from tests.test_realtime_setup import loaded as loaded, _clear_loader_state as _clear_loader_state, Wire
from tests.test_onboarding_process_controls import startup_controls as startup_controls


def transport_request(**overrides):
    return {**dict(kind="realtime", provider="openai_realtime:openai", model="gpt-realtime-2.1",
                   timeout_s=10, probe=dict(text="Return the synthetic room fact.", max_output_tokens=32)),
            **overrides}


def wait_for(predicate, timeout=10):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError("timed out waiting for consumer evidence")


@pytest.fixture
def consumer(tmp_path, loaded, monkeypatch, provider_catalog_offline, request):
    from ouroboros import config, context, realtime_setup, usage_accounting as ua
    from ouroboros.capability_evidence import record_owner_ack
    from ouroboros.response_limits import record_response_ack
    from ouroboros.model_wait import task_model_wait_scope
    from ouroboros.tools.tool_context import ToolContext
    from tests._memory_view_context import world

    env, memory, rooms = world(tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", env.drive_root)
    # The accounting density observer resolves the installation root from the
    # environment first. Keep that real writer and this fixture's reader aligned.
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(env.drive_root))
    monkeypatch.setattr(context, "get_context_mode", lambda: "nano")
    route = dict(provider="openai_realtime", model="openai::gpt-realtime-2.1",
                 base_url="wss://api.openai.com/v1/realtime")
    # Test assertions, not provider qualification or new host budget policy.
    record_owner_ack(env.drive_root, **route, window_tokens=128000, owner="synthetic fixture")
    record_response_ack(env.drive_root, **route, max_output_tokens=32000)
    events = []
    turn = RealtimeTurn(transport_request(), publish=events.append)
    task = dict(id="realtime-synthetic", type="task", text="Synthetic ordinary owner input.",
                chat_id=rooms["alpha"], _is_direct_chat=True,
                origin_message_ref={"chat_id": rooms["alpha"], "client_message_id": "synthetic"},
                model=turn.model, use_local_model=False)
    from ouroboros.task_results import write_task_result, STATUS_RUNNING
    from tests.test_budget_fence_liveness import _publish

    write_task_result(env.drive_root, task["id"], STATUS_RUNNING, chat_id=task["chat_id"])
    _publish(env.drive_root)
    ctx = ToolContext(repo_dir=env.repo_dir, drive_root=env.drive_root, branch_dev="test",
                      task_id=task["id"], current_chat_id=task["chat_id"], is_direct_chat=True,
                      task_metadata={}, task_contract={})
    tools = SimpleNamespace(schemas=lambda: [
        {"type": "function", "function": {"name": name, "description": name,
         "parameters": {"type": "object", "properties": {}}}}
        for name in ("read_file", "list_available_tools", "enable_tools", "compact_context")])
    messages, _ = context.build_llm_messages(
        env, memory, task, ctx=ctx, tool_schemas=tools.schemas(), route_resolver=turn.resolve_route)
    messages.append({"role": "user", "content": "LATE_EXECUTOR_NOTE 🐍\n"})
    original = realtime_setup.run_realtime_setup
    wire = Wire()

    async def injected(*args, **kwargs):
        return await original(*args, **{**kwargs, "connect": wire.connect})

    monkeypatch.setattr(realtime_setup, "run_realtime_setup", injected)
    # Normal physical-attempt ledger with explicit mock tariff/funds, never real pricing.
    monkeypatch.setattr(ua, "estimate_cost_optional", lambda *a, **k: .004)
    result, errors = [], []

    def run():
        with ua.usage_scope(state.scope), \
                task_model_wait_scope(task=task, drive_root=env.drive_root,
                                      event_queue=None, worker_slot_held=False) as owner:
            ctx.model_wait_context = owner
            owner.tool_context = ctx
            try:
                result.append(turn.run(ctx, messages, tools, task=task, initial_effort=""))
            except ua.BudgetExceeded as exc:
                errors.append(exc)

    thread = threading.Thread(target=run, name="synthetic-realtime-turn")
    state = SimpleNamespace(turn=turn, task=task, ctx=ctx, wire=wire, thread=thread, messages=messages,
                            events=events, result=result, errors=errors, ua=ua, env=env, tools=tools,
                            scope=ua.UsageScope(drive_root=env.drive_root, task_id=task["id"],
                                                root_task_id=task["id"], global_limit_usd=10))
    yield state
    if thread.is_alive():
        turn.deadline = time.monotonic()
        thread.join(10)
    if os.environ.get("OUROBOROS_TEST_EVIDENCE_ROOT"):
        from ouroboros.artifacts import task_artifact_dir_path

        evidence = Path(os.environ["OUROBOROS_TEST_EVIDENCE_ROOT"]) / "consumer" / re.sub(
            r"[^a-zA-Z0-9._-]", "_", request.node.name)
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "observations.json").write_text(json.dumps(dict(
            kind="synthetic-consumer-only", events=events, result=result,
            errors=[str(exc) for exc in errors], sent=wire.sent, socket_closed=wire.closed,
            actor_alive=thread.is_alive(), ledger=ua.read_usage_records(env.drive_root),
        ), ensure_ascii=False, indent=2))
        artifacts = task_artifact_dir_path(env.drive_root, task["id"])
        if artifacts.exists():
            shutil.copytree(artifacts, evidence / "artifacts", dirs_exist_ok=True)
    assert not thread.is_alive(), "consumer must join its provider and accounting work"


def prepared(c):
    c.thread.start()
    event = wait_for(lambda: next((e for e in c.events
                                  if e["type"] in {"realtime_prepared", "realtime_finished"}), None))
    assert event["type"] == "realtime_prepared", c.result or event
    return event


def test_full_prepared_input_normal_tools_and_funded_ledger(consumer):
    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.tool_policy import initial_tool_schemas

    c = consumer
    preview = prepared(c)
    capture = json.loads(read_actor_source_bytes(c.ctx.drive_root, c.ctx.task_id, preview["source_ref"]))
    assert json.loads(capture["selected"]["messages_json"]) == c.messages
    assert json.loads(capture["selected"]["tools_json"]) == initial_tool_schemas(c.tools, context_mode="nano")
    assert "LATE_EXECUTOR_NOTE" in capture["selected"]["messages_json"]
    assert capture["context_fit_plan"]["core"]["memory_view_json"]
    assert capture["context_fit_plan"]["provider"] == "openai_realtime"
    assert capture["context_fit_plan"]["window_tokens"] == 128000
    assert capture["context_fit_plan"]["output_reserve_tokens"] == 32000
    assert c.wire.sent == [] and c.ua.read_usage_records(c.ctx.drive_root) == []
    with pytest.raises(RealtimeSetupError, match="wire_changed"):
        c.turn.authorize("0" * 64, 1)
    c.turn.authorize(preview["wire_sha256"], .5)
    with pytest.raises(RealtimeSetupError, match="already_authorized"):
        c.turn.authorize(preview["wire_sha256"], .5)
    c.thread.join(10)
    assert c.result[0][1]["execution_status"] == "ok", c.result
    rows = c.ua.read_usage_records(c.ctx.drive_root, final_only=True)
    assert len(rows) == 1 and rows[0]["state"] == "settled"
    assert rows[0]["reservation_upper_bound_usd"] == .5 and rows[0]["cost_usd"] == .004
    assert rows[0]["task_id"] == c.task["id"]
    assert (rows[0]["prompt_tokens"], rows[0]["completion_tokens"]) == (123, 4)
    assert c.wire.closed
    # The funded send copy is sealed by the ordinary model_send owner, joined to its attempt.
    from ouroboros.model_send_seal import reconcile_model_send_seals
    from ouroboros.observability import read_call_manifest_ref

    ref = rows[0]["candidate_manifest_ref"]
    assert ref["model_send_seal_version"] == 1, rows[0]
    seal = read_call_manifest_ref(c.ctx.drive_root, ref, task_id=c.task["id"])["model_send_seal"]
    assert seal["attempt_id"] == rows[0]["attempt_id"]
    assert (seal["pre_redaction_sha256"], seal["size_bytes"]) == (
        preview["wire_sha256"], rows[0]["candidate_raw_size_bytes"])
    report = reconcile_model_send_seals(c.ctx.drive_root)
    assert report["status"] == "completed" and report["sealed_attempts"] == 1, report
    assert not (report["orphan_seals"] or report["unlogged_attempts"] or report.get("forward_divergences")), report


@pytest.mark.parametrize("mutation,code", [
    (lambda c: c.messages.append({"role": "tool", "content": "not owner"}), "role"),
    (lambda c: c.messages.append({"role": "user", "content": [{"type": "image_url", "image_url": {}}]}), "content"),
    (lambda c: setattr(c.ctx, "context_fit_plan", replace(c.ctx.context_fit_plan, initial_mode="invalid")), "context_mode"),
])
def test_unrepresentable_input_refuses_before_effect(consumer, mutation, code):
    c = consumer
    mutation(c)
    c.thread.start()
    c.thread.join(10)
    assert not c.wire.sent and c.ua.read_usage_records(c.ctx.drive_root) == []
    assert c.result[0][1]["reason_code"] == code, c.result


def test_stop_joins_slow_teardown_and_retains_loaded_revision(consumer):
    from contextlib import asynccontextmanager
    from ouroboros.owner_mailbox import write_owner_message, KIND_FINALIZE_NOW
    from ouroboros.extension_registry_state import _retiring_imports
    from ouroboros import extension_loader as loader

    c = consumer
    entered, release, closing = threading.Event(), threading.Event(), threading.Event()

    async def recv():
        entered.set()
        await asyncio.Event().wait()

    @asynccontextmanager
    async def connect(model):
        try:
            yield c.wire
        finally:
            closing.set()
            await asyncio.to_thread(release.wait, 5)
            c.wire.closed = True

    c.wire.recv, c.wire.connect = recv, connect
    preview = prepared(c)
    c.turn.authorize(preview["wire_sha256"], .5)
    assert entered.wait(5)
    write_owner_message(c.ctx.drive_root, "owner_stopped_direct_turn", c.task["id"],
                        kind=KIND_FINALIZE_NOW)
    assert closing.wait(5)
    loader.unload_extension("openai_realtime")
    assert c.thread.is_alive() and not c.wire.closed
    assert "openai_realtime" in _retiring_imports
    assert not any(e["type"] == "realtime_finished" for e in c.events)
    release.set()
    c.thread.join(10)
    assert c.wire.closed and not c.thread.is_alive()
    assert "openai_realtime" not in _retiring_imports
    assert c.result[0][1]["reason_code"] == "finalize_requested", c.result
    rows = c.ua.read_usage_records(c.ctx.drive_root, final_only=True)
    assert len(rows) == 1 and rows[0]["state"] == "unresolved" and rows[0].get("cost_usd") is None


@pytest.mark.parametrize("outcome", ["unknown", "failed", "late"])
def test_deadline_failed_and_late_usage_are_never_free(consumer, outcome):
    c = consumer
    if outcome == "failed":
        c.wire.alter = lambda event, _: (
            {**event, "response": {**event["response"], "status": "failed"}}
            if event["type"] == "response.done" else event)
    else:
        original = c.wire.recv

        async def held():
            event = await original()
            if json.loads(event)["type"] == "response.done":
                c.turn.deadline = time.monotonic() + .1
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    if outcome == "late":
                        return event
                    raise
            return event
        c.wire.recv = held
    preview = prepared(c)
    c.turn.authorize(preview["wire_sha256"], .5)
    c.thread.join(10)
    rows = c.ua.read_usage_records(c.ctx.drive_root, final_only=True)
    assert len(rows) == 1
    row = rows[0]
    assert row["state"] == ("unresolved" if outcome == "unknown" else "settled"), row
    assert row.get("cost_usd") == (None if outcome == "unknown" else .004)
    assert c.wire.closed
    assert c.result[0][1]["execution_status"] != "ok"


def test_unfunded_deadline_has_no_attempt(consumer):
    c = consumer
    prepared(c)
    for bad in (0, -1, float("nan"), float("inf"), True, "1"):
        with pytest.raises(RealtimeSetupError, match="funding"):
            c.turn.authorize(c.turn._wire_sha256, bad)
    c.turn.deadline = time.monotonic()
    c.thread.join(10)
    assert c.result[0][1]["reason_code"] == "deadline"
    assert not c.wire.sent and c.ua.read_usage_records(c.ctx.drive_root) == []


def test_funding_cannot_override_exhausted_host_budget(consumer):
    c = consumer
    c.scope = replace(c.scope, global_limit_usd=0)
    preview = prepared(c)
    c.turn.authorize(preview["wire_sha256"], .5)
    c.thread.join(10)
    assert len(c.errors) == 1 and isinstance(c.errors[0], c.ua.BudgetExceeded)
    assert not c.wire.sent and c.ua.read_usage_records(c.ctx.drive_root) == []
    assert c.events[-1]["reason"] == "budget_exhausted"


@pytest.mark.parametrize("fault,code", [("bytes", "item_mismatch"), ("role", "item_mismatch"),
                                       ("order", "item_order")])
def test_provider_ack_drift_refuses_probe_and_retains_exposure(consumer, fault, code):
    c = consumer

    def alter(event, outgoing):
        if event["type"] == "conversation.item.done":
            if fault == "bytes":
                event["item"]["content"][0]["text"] += "\nBYTE_DRIFT"
            elif fault == "role":
                event["item"]["role"] = "assistant"
            else:
                event["previous_item_id"] = "wrong-order"
        return event

    c.wire.alter = alter
    preview = prepared(c)
    c.turn.authorize(preview["wire_sha256"], .5)
    c.thread.join(10)
    assert c.result[0][1]["reason_code"] == code, c.result
    assert all(event["type"] != "response.create" for event in c.wire.sent)
    rows = c.ua.read_usage_records(c.ctx.drive_root, final_only=True)
    assert len(rows) == 1 and rows[0]["state"] == "unresolved" and rows[0].get("cost_usd") is None
    assert rows[0]["reservation_upper_bound_usd"] == .5 and c.wire.closed


def test_retired_preview_generation_refuses_before_reservation(consumer):
    from ouroboros import extension_loader as loader

    c = consumer
    preview = prepared(c)
    loader.unload_extension("openai_realtime")
    c.turn.authorize(preview["wire_sha256"], .5)
    c.thread.join(10)
    assert c.result[0][1]["reason_code"] == "provider_unavailable"
    assert not c.wire.sent and c.ua.read_usage_records(c.ctx.drive_root) == []


def test_replaced_preview_generation_refuses_before_reservation(consumer, monkeypatch):
    from ouroboros.extension_registry_state import _realtime_providers, _lock

    c = consumer
    preview = prepared(c)
    with monkeypatch.context() as patch:
        with _lock:
            patch.setitem(_realtime_providers, c.turn.provider, {
                **_realtime_providers[c.turn.provider], "extension_generation": "synthetic-new-generation"})
        c.turn.authorize(preview["wire_sha256"], .5)
        c.thread.join(10)
    assert c.result[0][1]["reason_code"] == "provider_changed"
    assert not c.wire.sent and c.ua.read_usage_records(c.ctx.drive_root) == []


def test_reported_usage_without_a_tariff_is_not_zero(consumer, monkeypatch):
    c = consumer
    monkeypatch.setattr(c.ua, "estimate_cost_optional", lambda *a, **k: None)
    preview = prepared(c)
    c.turn.authorize(preview["wire_sha256"], .5)
    c.thread.join(10)
    assert c.result[0][1]["execution_status"] == "ok"
    row, = c.ua.read_usage_records(c.ctx.drive_root, final_only=True)
    assert row["cost_usd"] is None and row["cost_final"] is False
    assert row["reservation_upper_bound_usd"] == .5
    assert c.wire.closed


def test_route_identity_comes_from_the_registered_provider(loaded, monkeypatch):
    """The consumer selects by registry key; accounting provider, endpoint and model prefix
    are the registration's declared facts, not names fixed in the host."""
    from ouroboros.contracts.realtime import RealtimeRoute
    from ouroboros.extension_registry_state import _lock, _realtime_providers

    route = RealtimeRoute(accounting_provider="synthetic_realtime", model_prefix="synthetic::",
                          endpoint="wss://synthetic.invalid/realtime")
    with _lock:
        monkeypatch.setitem(_realtime_providers, "openai_realtime:synthetic",
                            {**_realtime_providers["openai_realtime:openai"], "route": route})
    turn = RealtimeTurn(transport_request(provider="openai_realtime:synthetic"), publish=lambda _: None)
    assert (turn.model, turn.native_model) == ("synthetic::gpt-realtime-2.1", "gpt-realtime-2.1")
    selected, _ = turn.resolve_route({}, allow_fetch=False)
    assert (selected["provider"], selected["model"], selected["base_url"]) == (
        "synthetic_realtime", "synthetic::gpt-realtime-2.1", "wss://synthetic.invalid/realtime")
    with pytest.raises(RealtimeSetupError) as error:
        RealtimeTurn(transport_request(provider="openai_realtime:missing"), publish=lambda _: None)
    assert error.value.code == "provider_unavailable"


def test_realtime_actor_is_not_an_owner_message_reader_during_preparation(monkeypatch, tmp_path, loaded):
    """Nothing in this transport drains the owner mailbox, so admission never opens:
    Project room speech during preparation is not delivered to an actor that cannot read it."""
    from ouroboros import agent as module, realtime_turn

    repo, drive = tmp_path / "repo", tmp_path / "agent-drive"  # `loaded` owns tmp_path/drive
    repo.mkdir()
    drive.mkdir()
    monkeypatch.setattr(module.OuroborosAgent, "_log_worker_boot_once", lambda self: None)
    actor = module.OuroborosAgent(module.Env(repo_dir=repo, drive_root=drive))
    actor._realtime_turn = RealtimeTurn(transport_request(), publish=lambda _: None)
    seen = []

    def observe_preparation(task, turn):
        seen.append((actor._busy, actor._accepting_owner_messages))
        return "synthetic_preparation_observed"

    monkeypatch.setattr(realtime_turn, "recovery_refusal", observe_preparation)
    events = actor._handle_task_scoped({"id": "realtime-admission", "type": "task", "chat_id": 1,
                                        "text": "synthetic", "_is_direct_chat": True,
                                        "_skip_post_task_synthesis": True})
    assert seen == [(True, False)]
    assert any(event.get("type") == "task_done" for event in events)


def _inert_room_socket(startup_controls, monkeypatch):
    """The real `/ws` endpoint over a real bridge; owner commands are recorded, never run."""
    from supervisor import message_bus

    bridge = message_bus.LocalChatBridge()
    queued, commands = [], []
    monkeypatch.setattr(bridge, "enqueue_local_message", lambda text, **kwargs: queued.append((text, kwargs)))
    monkeypatch.setattr(message_bus, "_BRIDGE", bridge)
    startup_controls.app.state.startup_owner_command = lambda command, **kwargs: commands.append(command)
    monkeypatch.setattr(startup_controls.server, "_perform_owner_restart", lambda *_: pytest.fail("restart ran"))
    return queued, commands


def _realtime_frame_replies(client, content):
    with client.websocket_connect("/ws") as socket:
        socket.send_json(dict(type="chat", content=content, chat_id=1, client_message_id="realtime-room-text",
                              execution_transport=transport_request()))
        # Malformed funding frames get an immediate typed reply, ordering the replies.
        socket.send_json({"type": "realtime_execute"})
        socket.send_json({"type": "realtime_execute"})
        return [socket.receive_json(), socket.receive_json()]


@pytest.mark.serial
@pytest.mark.parametrize("content", ["/restart", "  /restart", "\n\t/RESTART "])
def test_ws_realtime_frame_refuses_slash_text_before_any_command_dispatch(startup_controls, monkeypatch, content):
    queued, commands = _inert_room_socket(startup_controls, monkeypatch)
    replies = _realtime_frame_replies(startup_controls.client, content)
    assert [(reply["type"], reply["code"]) for reply in replies] == [
        ("realtime_refused", "transport"), ("realtime_refused", "funding")], replies
    time.sleep(.5)  # a wrongly accepted frame would dispatch off the socket loop
    assert commands == [] and queued == []
    assert not (startup_controls.data / "logs/chat.jsonl").exists()


@pytest.mark.serial
def test_ws_realtime_frame_keeps_ordinary_text_with_leading_whitespace(startup_controls, monkeypatch, loaded):
    queued, commands = _inert_room_socket(startup_controls, monkeypatch)
    replies = _realtime_frame_replies(startup_controls.client, "  Synthetic room words, not a command.")
    assert [(reply["type"], reply.get("code")) for reply in replies] == [
        ("realtime_refused", "funding"), ("realtime_refused", "funding")], replies
    accepted = wait_for(lambda: queued)
    assert commands == [] and len(accepted) == 1
    text, message = accepted[0]
    assert text.strip() == "Synthetic room words, not a command."
    assert isinstance(message["task_metadata"]["_realtime_turn"], RealtimeTurn)
    assert message["task_metadata"]["execution_transport"] == transport_request()


def test_authority_refusal_precedes_transport(monkeypatch, tmp_path, loaded):
    from ouroboros.agent import Env, OuroborosAgent
    from ouroboros.project_dialogue import build_owner_message_ref
    from tests._store_shared import read_result

    repo, drive = tmp_path / "repo", tmp_path / "agent-drive"  # `loaded` owns tmp_path/drive
    repo.mkdir()
    drive.mkdir()
    monkeypatch.setattr(OuroborosAgent, "_log_worker_boot_once", lambda self: None)
    agent = OuroborosAgent(Env(repo_dir=repo, drive_root=drive))
    turn = RealtimeTurn(transport_request(), publish=lambda _: pytest.fail("refused actor reached transport"))
    agent._realtime_turn = turn
    events = agent._handle_task_scoped({
        "id": "missing-realtime-origin", "type": "task", "chat_id": 1, "text": "synthetic",
        "_is_direct_chat": True, "_skip_post_task_synthesis": True,
        "origin_message_ref": build_owner_message_ref(
            chat_id=1, client_message_id="missing", ts="2026-01-01T00:00:00Z", text="missing"),
    })
    assert any(event.get("type") == "task_done" for event in events)
    assert read_result(drive, "missing-realtime-origin")["reason_code"] == "authority_source_unavailable"


@pytest.mark.parametrize("recovery", ["pause", "restart"])
def test_reconstructed_task_refuses_before_context_or_text_dispatch(monkeypatch, tmp_path, recovery):
    from ouroboros import agent as module
    from ouroboros.project_dialogue import build_owner_message_ref
    from ouroboros.task_results import load_task_result
    from ouroboros.usage_accounting import read_usage_records

    repo, drive = tmp_path / "repo", tmp_path / "drive"
    repo.mkdir()
    drive.mkdir()
    monkeypatch.setattr(module.OuroborosAgent, "_log_worker_boot_once", lambda self: None)
    def forbidden(*args, **kwargs):
        pytest.fail("a recovered realtime intent reached cognition or a text dispatch")
    monkeypatch.setattr(module, "build_llm_messages", forbidden)
    monkeypatch.setattr(module, "run_llm_loop", forbidden)
    actor = module.OuroborosAgent(module.Env(repo_dir=repo, drive_root=drive))
    text = "Synthetic retained ordinary owner input."
    task = dict(id="reconstructed-" + recovery, type="task", text=text, chat_id=1,
                origin_message_text=text, origin_message_ref=build_owner_message_ref(
                    chat_id=1, client_message_id="synthetic", ts="2026-01-01T00:00:00Z", text=text),
                metadata={"execution_transport": transport_request()})
    events = actor._handle_task_scoped(task)
    result = load_task_result(drive, task["id"])
    assert result["reason_code"] == "realtime_restart_required"
    assert result["metadata"]["execution_transport"] == task["metadata"]["execution_transport"]
    assert any(event.get("type") == "task_done" for event in events)
    assert read_usage_records(drive) == []


def test_continue_refuses_realtime_intent_without_admitting_a_successor(monkeypatch, tmp_path):
    from tests.test_owner_continue import _install_queue, _interrupted, NONCE
    from supervisor.continuation_admission import admit_continuation

    queue, _, _ = _install_queue(tmp_path, monkeypatch)
    _interrupted(tmp_path, metadata={"execution_transport": transport_request()})
    result = admit_continuation("pred-1", action_nonce=NONCE)
    assert result["error"] == "realtime_restart_required", result
    assert queue.PENDING == [] and queue.RUNNING == {}


@pytest.mark.parametrize("accepting", [True, False])
def test_pause_refuses_live_transport_before_writing_a_fence(monkeypatch, tmp_path, accepting, loaded):
    from tests.test_direct_chat_turn_owner_control import _isolate_queue, _live_chat_agent, TURN_ID
    from ouroboros.owner_pause import read_fence
    from ouroboros.task_results import write_task_result
    from supervisor.owner_pause_control import request_owner_pause

    _isolate_queue(monkeypatch, tmp_path)
    actor = _live_chat_agent(monkeypatch, accepting=accepting)
    actor._realtime_turn = RealtimeTurn(transport_request(), publish=lambda _: None)
    write_task_result(tmp_path, TURN_ID, "running")
    assert request_owner_pause(TURN_ID, request_id="synthetic-pause") == {
        "ok": False, "error": "realtime_pause_unsupported"}
    assert read_fence(tmp_path, TURN_ID) == {}
