"""Synthetic offline consumers, through the real loader and ordinary context builder.

No current-owner capture, credentials, API connection, device or self-review.
The PASS/enable markers below belong only to disposable test installations.
"""
from __future__ import annotations

import asyncio
import copy
import importlib
import json
import pathlib
import shutil
import sys
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from ouroboros import extension_loader as loader
from ouroboros.contracts.plugin_api import ExtensionRegistrationError, negotiate_plugin_api, ExecutionMode
from ouroboros.contracts.realtime import RealtimeInput, RealtimeProbe, RealtimeSetupError, canonical_json, sha256
from ouroboros.extension_import_staging import _module_key
from ouroboros.extension_registry_state import _realtime_providers, _retiring_imports
from ouroboros.realtime_setup import capture_realtime_input, input_from_context_plan, run_realtime_setup
from ouroboros.skill_loader import find_skill, save_enabled, SkillReviewState
from tests._extension_loader_shared import _clear_loader_state  # noqa: F401

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROVIDER = "openai_realtime:openai"


@pytest.fixture
def loaded(tmp_path):
    directory = tmp_path / "skills" / "openai_realtime"
    shutil.copytree(ROOT / "skills/openai_realtime", directory)
    (directory / "late_identity.py").write_text('VALUE = "synthetic-loaded-revision"\n')
    drive = tmp_path / "drive"
    drive.mkdir()
    skill = find_skill(drive, "openai_realtime", repo_path=str(directory.parent))
    save_enabled(drive, skill.name, True)
    # Protected-key grants are deliberately absent: preview and injected wires
    # never need a credential. The fixture's manifest tests the setup permission.
    manifest = replace(skill.manifest, env_from_settings=[], permissions=["realtime_provider"])
    skill = replace(skill, manifest=manifest, enabled=True,
                    review=SkillReviewState(status="pass", content_hash=skill.content_hash))
    # Same admission predicate as ordinary extensions; no review of our live package.
    assert negotiate_plugin_api(skill.manifest).ok
    assert loader.load_extension(skill, lambda: {}, drive_root=drive) is None
    yield skill, drive
    loader.unload_extension(skill.name)


def selected():
    # Labelled synthetic wire input. The ordinary-capture test below does not
    # use this direct constructor: it builds a real plan over disposable stores.
    return RealtimeInput(
        model="gpt-realtime-2.1", mode="nano", core_sha256="synthetic-core",
        memory_view_sha256="synthetic-view", output_allowance_tokens=8192,
        messages_json=canonical_json([
            {"role": "system", "content": [
                {"type": "text", "text": "  GOVERNANCE 🐍\n", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "IDENTITY\n"},
                {"type": "text", "text": "Room: [foreign speaker, quoted] не owner\n"},
            ], "_stable_prefix_blocks": 2},
            {"role": "user", "content": "Actual admitted owner input.\n"},
            {"role": "assistant", "content": "Earlier answer.\n"},
            {"role": "system", "content": "Host authority.\n"},
        ]),
        tools_json=canonical_json([{"type": "function", "function": {
            "name": "read_file", "description": "Read exactly.", "parameters": {"type": "object",
            "properties": {"path": {"type": "string"}}, "required": ["path"]},
        }}]),
    )


def documented_output_limit(value):
    """The documented session/response ``max_output_tokens`` grammar: an integer 1-4096 or
    ``"inf"`` (OpenAI API reference, Realtime, read 2026-10-20). Model output capability is
    separate route evidence; this synthetic peer refuses the field the way the API says it may."""
    return value == "inf" or (type(value) is int and 1 <= value <= 4096)


class Wire:
    def __init__(self, alter=None):
        self.sent, self.events = [], asyncio.Queue()
        self.alter = alter
        self.closed = False

    async def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        limit = (event.get("session") or event.get("response") or {}).get("max_output_tokens", "inf")
        if event["type"] in {"session.update", "response.create"} and not documented_output_limit(limit):
            reply = {"type": "error", "error": {"type": "invalid_request_error",
                                                "param": "max_output_tokens", "event_id": event["event_id"]}}
        elif event["type"] == "session.update":
            reply = {"type": "session.updated", "session": copy.deepcopy(event["session"])}
        elif event["type"] == "conversation.item.create":
            reply = {"type": "conversation.item.done", "item": copy.deepcopy(event["item"]),
                     "previous_item_id": None if event["previous_item_id"] == "root" else event["previous_item_id"]}
        else:
            await self.events.put(canonical_json({
                "type": "response.created", "response": {"id": "r_synthetic"},
            }))
            reply = {"type": "response.done", "response": {"id": "r_synthetic", "status": "completed",
                     "output": [], "usage": {"input_tokens": 123, "output_tokens": 4}}}
        if self.alter:
            reply = self.alter(reply, event)
        if reply is not None:
            await self.events.put(canonical_json(reply))

    async def recv(self):
        return await self.events.get()

    @asynccontextmanager
    async def connect(self, model):
        assert model == "gpt-realtime-2.1"
        try:
            yield self
        finally:
            self.closed = True


async def preview(value=None, probe=None):
    return await run_realtime_setup(PROVIDER, value or selected(), timeout_s=2,
                                    observe=lambda *args: None, probe=probe)


async def execute(wire, value=None, probe=None, *, timeout=2):
    value = value or selected()
    plan = await preview(value, probe)
    observed = []
    try:
        result = await run_realtime_setup(
            PROVIDER, value, timeout_s=timeout, observe=lambda *row: observed.append(row),
            execute=True, expected_wire_sha256=plan.wire_sha256, probe=probe, connect=wire.connect,
        )
    except BaseException:
        print("SYNTHETIC_WIRE_FAILURE", canonical_json(observed))
        raise
    return result, observed


def test_real_manifest_requires_in_process_provider_and_declares_granted_key():
    from ouroboros.contracts.skill_manifest import parse_skill_manifest_text

    manifest = parse_skill_manifest_text((ROOT / "skills/openai_realtime/SKILL.md").read_text())
    assert manifest.env_from_settings == ["OPENAI_API_KEY"]
    assert "realtime_provider" in manifest.permissions
    assert negotiate_plugin_api(manifest).ok
    assert not negotiate_plugin_api(manifest, mode=ExecutionMode.OUT_OF_PROCESS).ok


def test_registration_is_permission_gated_atomic_and_rejects_child(tmp_path, monkeypatch):
    from ouroboros.contracts.realtime import RealtimeRoute

    async def handler(*args, **kwargs):
        pass

    route = RealtimeRoute(accounting_provider="atomic_realtime", endpoint="wss://atomic.invalid",
                          model_prefix="atomic::")
    api = loader.PluginAPIImpl(loader._PluginAPIConfig(
        skill_name="atomic", permissions=[], env_allowlist=[], state_dir=tmp_path, settings_reader=lambda: {},
    ))
    with pytest.raises(ExtensionRegistrationError):
        api.register_realtime_provider("openai", handler, route=route)
    api._permissions = frozenset(["realtime_provider"])
    with pytest.raises(ExtensionRegistrationError, match="RealtimeRoute"):
        api.register_realtime_provider("openai", handler, route={"accounting_provider": "atomic_realtime"})
    api.register_realtime_provider("openai", handler, route=route)
    assert "atomic:openai" not in _realtime_providers
    assert api._staged.realtime_providers["atomic:openai"]["route"] is route
    with pytest.raises(ExtensionRegistrationError):
        api.register_realtime_provider("openai", handler, route=route)
    api._abort_registration()
    assert "atomic:openai" not in _realtime_providers
    monkeypatch.setenv("OUROBOROS_EXTENSION_PROCESS_CHILD", "1")
    with pytest.raises(ExtensionRegistrationError, match="not available"):
        api.register_realtime_provider("openai", handler, route=route)


def test_child_catalog_cannot_smuggle_an_in_process_provider(loaded):
    from ouroboros.extension_child_catalog import _stage_out_of_process_surfaces

    skill, _ = loaded
    api = loader._extensions[skill.name].api_instances[0]
    with pytest.raises(ExtensionRegistrationError, match="in-process"):
        _stage_out_of_process_surfaces(api, skill, {"realtime_providers": [{"name": "openai"}]})


def test_preview_and_consumer_preserve_all_text_roles_tools_and_usage(loaded):
    async def scenario():
        wire = Wire()
        probe = RealtimeProbe("Report the recalled content.", 256)
        prepared = await preview(probe=probe)
        result, observed = await execute(wire, probe=probe)
        events = json.loads(prepared.wire_json)["events"]
        assert wire.sent == events
        before = json.loads(selected().messages_json)
        expected = [(msg["role"], part["text"]) for msg in before
                    for part in (msg["content"] if isinstance(msg["content"], list)
                                 else [{"text": msg["content"]}])]
        after = [("system", events[0]["session"]["instructions"])] + [
            (e["item"]["role"], part["text"]) for e in events[1:-2] for part in e["item"]["content"]]
        assert after == expected
        assert events[0]["session"]["tools"] == [
            {"type": "function", **json.loads(selected().tools_json)[0]["function"]}]
        assert events[4]["item"]["content"][0]["type"] == "output_text"
        assert result.state == "response_completed" and wire.closed
        assert json.loads(result.response_json)["usage"]["input_tokens"] == 123
        # The provider maps its own usage grammar to host axes; the raw event stays beside it.
        assert [json.loads(raw) for direction, raw in observed if direction == "usage"] == [
            {"prompt_tokens": 123, "completion_tokens": 4}]
        print("SYNTHETIC_CONSUMER", canonical_json({
            "wire_sha256": result.wire_sha256, "state": result.state, "events": observed,
        }))
    asyncio.run(scenario())


def test_output_fields_follow_the_documented_grammar_without_shrinking_the_host_allowance(loaded):
    value = selected()  # the ordinary allowance (8192) is above the field's integer range
    probe = RealtimeProbe("Check.", 256)
    events = json.loads(asyncio.run(preview(value, probe)).wire_json)["events"]
    assert events[0]["session"]["max_output_tokens"] == "inf"
    assert events[-1]["type"] == "response.create" and events[-1]["response"]["max_output_tokens"] == 256
    assert value.output_allowance_tokens == 8192
    for refused in (RealtimeProbe("Check.", 4097), RealtimeProbe("Check.", 9000)):
        with pytest.raises(RealtimeSetupError) as error:  # offline, before any funding or socket
            asyncio.run(preview(value, refused))
        assert error.value.code == "probe"
    with pytest.raises(RealtimeSetupError, match="allowance"):
        asyncio.run(preview(replace(value, output_allowance_tokens=128), probe))


def test_large_unicode_blocks_are_not_clipped_or_split_by_historical_caps(loaded):
    value = selected()
    messages = json.loads(value.messages_json)
    messages[0]["content"][0]["text"] = "Я🐍\n" * 40_000
    messages[0]["content"][1]["text"] = "MEMORY_END\n" * 10_000
    value = replace(value, messages_json=canonical_json(messages))
    wire = json.loads(asyncio.run(preview(value)).wire_json)["events"]
    assert wire[0]["session"]["instructions"] == messages[0]["content"][0]["text"]
    assert wire[1]["item"]["content"][0]["text"] == messages[0]["content"][1]["text"]


@pytest.mark.parametrize("mutate,code", [
    (lambda m: m[1].update(role="developer"), "role"),
    (lambda m: m[1].update(role="tool"), "role"),
    (lambda m: m[1].update(name="foreign-speaker"), "message_shape"),
    (lambda m: m[1].update(content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]), "content"),
    (lambda m: m[0]["content"].append({"type": "audio", "data": "AA"}), "content"),
    (lambda m: m[2].update(tool_calls=[{"id": "call"}]), "message_shape"),
])
def test_unsupported_selected_content_refuses_before_any_send(loaded, mutate, code):
    value = selected()
    messages = json.loads(value.messages_json)
    mutate(messages)
    value = replace(value, messages_json=canonical_json(messages))
    with pytest.raises(RealtimeSetupError) as error:
        asyncio.run(preview(value))
    assert error.value.code == code
    print("SYNTHETIC_NEGATIVE", code)


@pytest.mark.parametrize("fault,code", [
    ("truncation", "session_mismatch"), ("instructions", "session_mismatch"),
    ("tools", "session_mismatch"), ("role", "item_mismatch"), ("text", "item_mismatch"),
    ("order", "item_order"), ("error", "provider_error"), ("dropped", "provider_truncation"),
    ("auto_response", "unexpected_response"), ("failed_response", "response_failed"),
    ("wrong_response", "response_mismatch"), ("extra_system", "unexpected_item"),
])
def test_negative_wire_evidence_stops_without_refit_retry_or_early_probe(loaded, fault, code):
    def alter(reply, outgoing):
        if fault in {"truncation", "instructions", "tools"} and reply["type"] == "session.updated":
            reply["session"][fault] = {"truncation": "auto", "instructions": "cut", "tools": []}[fault]
        if reply["type"] == "conversation.item.done":
            if fault == "role":
                reply["item"]["role"] = "user"
            if fault == "text":
                reply["item"]["content"][0]["text"] = "cut"
            if fault == "order":
                reply["previous_item_id"] = "other"
            if fault == "error":
                reply = {"type": "error", "error": {"code": "context_length_exceeded", "event_id": outgoing["event_id"]}}
            if fault == "dropped":
                reply = {"type": "conversation.item.deleted", "item_id": "setup_item_1"}
            if fault == "auto_response":
                reply = {"type": "response.created", "response": {"id": "unrequested"}}
            if fault == "extra_system":
                reply["item"]["id"] = "unrequested"
        if fault == "failed_response" and reply["type"] == "response.done":
            reply["response"]["status"] = "failed"
        if fault == "wrong_response" and reply["type"] == "response.done":
            reply["response"]["id"] = "another_response"
        return reply

    wire = Wire(alter)
    with pytest.raises(RealtimeSetupError) as error:
        asyncio.run(execute(wire, probe=RealtimeProbe("Check.", 256)))
    assert error.value.code == code and wire.closed
    if fault not in {"failed_response", "wrong_response"}:
        assert not any(e["type"] == "response.create" for e in wire.sent)
    print("SYNTHETIC_NEGATIVE", fault, code, "sent", len(wire.sent))


def test_preview_digest_and_unsupported_schema_refuse_before_connect(loaded):
    async def scenario():
        value = selected()
        with pytest.raises(RealtimeSetupError, match="wire_changed"):
            await run_realtime_setup(PROVIDER, value, timeout_s=2, observe=lambda *a: None,
                                     execute=True, expected_wire_sha256="stale")
        tools = json.loads(value.tools_json)
        tools[0]["function"]["strict"] = True
        with pytest.raises(RealtimeSetupError, match="tools"):
            await preview(replace(value, tools_json=canonical_json(tools)))
    asyncio.run(scenario())


def test_absent_credential_fails_without_network_after_offline_preview(loaded):
    async def scenario():
        plan = await preview()
        with pytest.raises(RealtimeSetupError, match="credential"):
            await run_realtime_setup(
                PROVIDER, selected(), timeout_s=2, observe=lambda *a: None,
                execute=True, expected_wire_sha256=plan.wire_sha256,
            )
    asyncio.run(scenario())


def test_production_connector_reads_only_granted_reference_at_execute(loaded, monkeypatch):
    import websockets.legacy.client

    skill, _ = loaded
    api = loader._extensions[skill.name].api_instances[0]
    calls, connections = [], []
    wire = Wire()

    def synthetic_settings(keys):
        calls.append(keys)
        return {"OPENAI_API_KEY": "synthetic-credential-never-a-real-key"}

    def connect(url, **kwargs):
        connections.append((url, kwargs))
        return wire.connect("gpt-realtime-2.1")

    monkeypatch.setattr(api, "get_settings", synthetic_settings)
    monkeypatch.setattr(websockets.legacy.client, "connect", connect)
    async def scenario():
        prepared = await preview()
        assert not calls and not connections
        observed = []
        result = await run_realtime_setup(
            PROVIDER, selected(), timeout_s=2, observe=lambda *row: observed.append(row),
            execute=True, expected_wire_sha256=prepared.wire_sha256,
        )
        assert result.state == "setup_accepted" and wire.closed
        assert calls == [["OPENAI_API_KEY"]]
        assert connections[0][0] == "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1"
        assert connections[0][1]["extra_headers"] == {
            "Authorization": "Bearer synthetic-credential-never-a-real-key",
        }
        assert "synthetic-credential" not in canonical_json(observed) + result.wire_json
    asyncio.run(scenario())


def test_missing_ack_deadline_closes_transport_without_response(loaded):
    wire = Wire(lambda reply, outgoing: None)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(execute(wire, timeout=.02))
    assert wire.closed and len(wire.sent) == 1


def test_unload_retains_imports_through_repeated_stop_and_slow_close(loaded):
    skill, drive = loaded
    module_name = _module_key(skill.name)
    async def scenario():
        closing, finish = asyncio.Event(), asyncio.Event()
        wire = Wire(lambda reply, outgoing: None)
        @asynccontextmanager
        async def connect(model):
            try:
                yield wire
            finally:
                closing.set()
                await finish.wait()
                # A late sibling import still resolves the loaded revision.
                sibling = importlib.import_module(module_name + ".late_identity")
                assert sibling.VALUE == "synthetic-loaded-revision"
                assert pathlib.Path(sibling.__file__).is_relative_to(import_root)
                wire.closed = True
        plan = await preview()
        operation = asyncio.create_task(run_realtime_setup(
            PROVIDER, selected(), timeout_s=2, observe=lambda *a: None, connect=connect,
            execute=True, expected_wire_sha256=plan.wire_sha256,
        ))
        while not wire.sent:
            await asyncio.sleep(0)
        import_root = pathlib.Path(loader._extensions[skill.name].import_root)
        loader.unload_extension(skill.name)
        await closing.wait()
        assert PROVIDER not in _realtime_providers
        assert skill.name in _retiring_imports and module_name in sys.modules and import_root.exists()
        assert "still closing" in loader.load_extension(skill, lambda: {}, drive_root=drive)
        operation.cancel()
        operation.cancel()
        loader.unload_extension(skill.name)
        await asyncio.sleep(0)
        assert not operation.done() and module_name in sys.modules
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert wire.closed and skill.name not in _retiring_imports and module_name not in sys.modules
        assert module_name + ".late_identity" not in sys.modules
        assert loader.load_extension(skill, lambda: {}, drive_root=drive) is None
    asyncio.run(scenario())


def test_reconcile_during_import_drain_keeps_enable_and_requeues_after_release(loaded):
    """An enable/update reconcile racing a draining operation is a typed wait, not a load failure."""
    from ouroboros.extension_reconcile_queue import list_extension_reconcile_requests
    from ouroboros.extension_registry_state import _load_failures
    from ouroboros.skill_loader import load_enabled

    skill, drive = loaded

    def reconcile(**kwargs):
        return loader.reconcile_extension(skill.name, drive, lambda: {}, skills=[skill],
                                          selected_skill=skill, **kwargs)

    def requeued():  # beside the ordinary worker -> server announcement of each reconcile
        return [row["skill"] for row in list_extension_reconcile_requests(drive) if row["reason"] == "imports_released"]

    async def scenario():
        closing, finish = asyncio.Event(), asyncio.Event()
        wire = Wire(lambda reply, outgoing: None)

        @asynccontextmanager
        async def connect(model):
            try:
                yield wire
            finally:
                closing.set()
                await finish.wait()
                wire.closed = True
        plan = await preview()
        operation = asyncio.create_task(run_realtime_setup(
            PROVIDER, selected(), timeout_s=30, observe=lambda *a: None, connect=connect,
            execute=True, expected_wire_sha256=plan.wire_sha256,
        ))
        while not wire.sent:
            await asyncio.sleep(0)
        loader.unload_extension(skill.name)  # disable/update: dispatch removed, Stop requested
        await closing.wait()
        state = reconcile(revert_enabled_on_error=True)
        assert (state["action"], state["reason"]) == ("extension_reload_pending", "imports_retiring"), state
        assert skill.name not in _load_failures and not state.get("reverted_enabled")
        assert load_enabled(drive, skill.name) is True
        assert requeued() == []
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert requeued() == [skill.name]
        assert reconcile()["action"] == "extension_loaded"
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["max", "low", "nano"])
def test_ordinary_whole_capture_reaches_provider_unchanged(tmp_path, monkeypatch, loaded, mode):
    from tests._memory_view_context import world
    from ouroboros.tool_policy import select_tool_schemas
    from ouroboros.tools.tool_context import ToolContext

    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE", mode)
    env, memory, _ = world(tmp_path / "ordinary")
    ctx = ToolContext(repo_dir=env.repo_dir, drive_root=memory.drive_root, task_id="synthetic-realtime",
                      task_metadata={}, current_chat_id=1)
    schemas = json.loads(selected().tools_json)
    value = capture_realtime_input(
        env, memory, {"id": "synthetic-realtime", "type": "task", "text": "Synthetic owner input", "chat_id": 1},
        ctx=ctx, model="gpt-realtime-2.1", tool_schemas=schemas, output_allowance_tokens=8192,
    )
    plan = ctx.context_fit_plan
    assert plan.core.memory_view_json and plan.initial_mode == mode
    assert json.loads(value.messages_json) == plan.messages_for(mode)
    assert json.loads(value.tools_json) == list(select_tool_schemas(schemas, context_mode=mode).schemas)
    assert value.memory_view_sha256 == sha256(plan.core.memory_view_json)
    wire = Wire()
    result, _ = asyncio.run(execute(wire, value))
    assert result.state == "setup_accepted" and wire.closed
    assert wire.sent == json.loads(result.wire_json)["events"]
    with pytest.raises(RealtimeSetupError, match="missing_memory_view"):
        input_from_context_plan(replace(plan, core=replace(plan.core, memory_view_json="")),
                                model=value.model, selected_tools=[], output_allowance_tokens=8192)
    with pytest.raises(RealtimeSetupError, match="context_mode"):
        input_from_context_plan(replace(plan, initial_mode="nano", nano_projection=None),
                                model=value.model, selected_tools=[], output_allowance_tokens=8192)
    print("SYNTHETIC_ORDINARY_CAPTURE", mode, value.sha256, value.memory_view_sha256)
