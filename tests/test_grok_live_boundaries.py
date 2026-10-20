"""Observed xAI representations through the loaded adapter, synthetic wires only."""
from __future__ import annotations

import asyncio
import base64
import copy
import importlib
import json
import os
from pathlib import Path

import pytest

from tests._extension_loader_shared import _clear_loader_state  # noqa: F401
from tests.test_live_conversation import VoiceWire, packages as packages
from ouroboros.contracts.live_conversation import ContextUpdate, Interrupt, LiveOptions
from ouroboros.contracts.realtime import RealtimeInput, RealtimeSetupError, canonical_json


@pytest.fixture
def captured(request):
    records = []
    yield records
    root = Path(os.environ["OUROBOROS_TEST_EVIDENCE_ROOT"]) / "grok-boundaries"
    root.mkdir(parents=True, exist_ok=True)
    (root / (request.node.name + ".json")).write_text(json.dumps(records, indent=2, default=str))


def make(wire, captured, messages=None):
    from ouroboros.realtime_setup import loaded_provider
    selected = RealtimeInput("synthetic-grok", canonical_json(messages or [
        {"role": "system", "content": "Synthetic ordinary rules."},
        {"role": "user", "content": "Synthetic owner words."},
    ]), "[]", 256, "nano", "core", "view")
    wire.answer = lambda: None
    return loaded_provider("grok_realtime:grok")["session"](
        selected, LiveOptions(), connect=wire.connect,
        observe=lambda direction, raw: captured.append({"direction": direction, "raw": raw}))


class AddedWire(VoiceWire):
    fault = ""

    def __init__(self):
        super().__init__("grok", None)

    def put(self, value):
        value = copy.deepcopy(value)
        if value.get("type") == "conversation.item.done":
            value["type"] = "conversation.item.added"
            item = value["item"]
            item["status"] = "in_progress" if self.fault == "in_progress" else "completed"
            if self.fault == "id":
                item["id"] = "wrong-id"
            elif self.fault == "role":
                item["role"] = "assistant"
            elif self.fault == "text":
                item["content"][0]["text"] = "Changed words"
        super().put(value)


@pytest.mark.parametrize("phase", ["setup", "live"])
@pytest.mark.parametrize("fault", ["", "in_progress", "id", "role", "text"])
def test_added_ack_requires_completed_exact_item(packages, captured, phase, fault):
    async def run():
        wire = AddedWire()
        adapter = make(wire, captured)
        opening = None
        try:
            if phase == "setup":
                wire.fault = fault
                opening = asyncio.create_task(adapter.open())
                if fault in {"in_progress", "id"}:
                    done, _ = await asyncio.wait((opening,), timeout=.05)
                    assert not done
                    assert not any(row.get("session", {}).get("turn_detection") for row in wire.sent)
                elif fault:
                    with pytest.raises(RealtimeSetupError, match="item_mismatch"):
                        await asyncio.wait_for(opening, 1)
                else:
                    await asyncio.wait_for(opening, 1)
                    assert (await adapter.receive())[0].kind == "ready"
            else:
                await adapter.open()
                await adapter.receive()
                wire.fault = fault
                await adapter.send(ContextUpdate(({"role": "user", "content": "Exact new words"},)))
                if fault in {"role", "text"}:
                    with pytest.raises(RealtimeSetupError, match="item_mismatch"):
                        await adapter.receive()
                else:
                    await adapter.receive()
                    assert bool(adapter.outstanding) is bool(fault)
                    assert any(row.get("type") == "response.create" for row in wire.sent) is (not fault)
            if fault:
                assert not any(row.get("type") == "response.create" for row in wire.sent)
        finally:
            if opening is not None and not opening.done():
                opening.cancel()
                await asyncio.gather(opening, return_exceptions=True)
            await adapter.close()
    asyncio.run(run())


@pytest.mark.parametrize("fault,code", [
    ("response.created", "setup_response"),
    ("conversation.item.deleted", "provider_truncation"),
    ("conversation.item.truncated", "provider_truncation"),
    ("disabled", "session_mismatch"),
])
def test_vad_activation_keeps_admission_guards(packages, captured, fault, code):
    class VadWire(AddedWire):
        def put(self, value):
            if value.get("type") == "session.updated" and value.get("session", {}).get("turn_detection"):
                value = ({"type": "session.updated", "session": {"turn_detection": None}}
                         if fault == "disabled" else {"type": fault})
            super().put(value)

    async def run():
        adapter = make(VadWire(), captured)
        try:
            with pytest.raises(RealtimeSetupError, match=code):
                await asyncio.wait_for(adapter.open(), 1)
        finally:
            await adapter.close()
    asyncio.run(run())


def test_cancel_before_audio_drains_usage_and_allows_next_response(packages, captured):
    async def run():
        wire = AddedWire()
        adapter = make(wire, captured)
        try:
            await adapter.open()
            adapter.convert({"type": "response.created", "response": {"id": "cancelled"}})
            await adapter.send(Interrupt())
            audio = {"type": "response.audio.delta", "response_id": "cancelled", "item_id": "late",
                     "delta": base64.b64encode(b"\x00\x00" * 20).decode()}
            assert adapter.convert(audio) == []
            usage = adapter.convert({"type": "response.done", "usage": {"audio_seconds": 4},
                                     "response": {"id": "cancelled", "status": "cancelled"}})
            assert usage[0].data["response_id"] == "cancelled"
            assert usage[0].data["status"] == "cancelled"
            assert usage[0].data["raw_usage"] == {"audio_seconds": 4}
            adapter.convert({"type": "response.created", "response": {"id": "next"}})
            assert adapter.convert({**audio, "response_id": "next"})[0].kind == "audio"
            captured.append({"usage": usage})
        finally:
            await adapter.close()
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["archived", "live"])
def test_system_image_refuses_before_any_wire_write(packages, captured, phase):
    image = {"role": "system", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}]}
    wire = AddedWire()
    if phase == "archived":
        with pytest.raises(RealtimeSetupError, match="system_content"):
            make(wire, captured, [{"role": "system", "content": "rules"},
                                 {"role": "user", "content": "history"}, image])
    else:
        adapter = make(wire, captured)
        with pytest.raises(RealtimeSetupError, match="system_content"):
            asyncio.run(adapter.send(ContextUpdate((image,))))
    assert not wire.sent


@pytest.mark.parametrize("mutate", [False, True])
def test_parent_echo_representation_preserves_schema_checks(packages, captured, mutate):
    from ouroboros.extension_import_staging import _module_key
    module = importlib.import_module(_module_key("grok_realtime"))
    requested = {
        "tools": [{"type": "function", "name": "read_file", "parameters": {"type": "object"}}],
        "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}}},
    }
    actual = {
        "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}],
        "audio": {"input": {"transport": "json", "format": {"type": "audio/pcm", "rate": 24000}}},
    }
    if mutate:
        actual["tools"][0]["function"]["parameters"] = {"type": "string"}
        with pytest.raises(RealtimeSetupError, match="session_mismatch"):
            module.check_session_echo(requested, actual)
    else:
        module.check_session_echo(requested, actual)
    captured.append({"requested": requested, "actual": actual})
