"""Recovered peer codec and Presence attribution using disposable synthetic data."""
from __future__ import annotations

import asyncio
import json
import struct

import pytest

from ouroboros import call_protocol as cp
from ouroboros.call_peers import LiveCallHub
from ouroboros.gateway.call_ws import serve_peer


def test_mixed_audio_and_meeting_bye_keep_nonowner_provenance():
    hub = LiveCallHub(cp.CallSpec("synthetic-mix", 1, "synthetic", "synthetic"), rates=(24000, 24000))
    for name, role in (("owner", cp.PEER_OWNER), ("guest", cp.PEER_MEETING)):
        hub.join(cp.PeerInfo(name, role, True, True), 24000)
        assert isinstance(hub.next_event(0), cp.PeerJoined)
        frame = cp.AudioFrame(cp.FRAME_AUDIO_IN, 0, 1, 24000, 0, struct.pack("<480h", *([1000] * 480)))
        assert hub.receive_audio(name, cp.decode_frame(cp.encode_frame(frame)))
    mixed = hub.next_event(.1)
    assert isinstance(mixed, cp.PeerAudio) and mixed.role == cp.PEER_MEETING
    assert struct.unpack("<480h", mixed.pcm) == (2000,) * 480
    hub.receive_control("guest", {"type": "bye"})
    assert not hub.ended_by_owner
    hub.receive_control("owner", {"type": "bye"})
    assert hub.ended_by_owner
    hub.close("fixture_cleanup")


def test_shared_socket_parser_refuses_peer_role_escalation():
    class Socket:
        def __init__(self):
            self.closed = []
        async def accept(self):
            pass
        async def receive(self):
            return {"type": "websocket.receive", "text": json.dumps({
                "type": "hello", "protocol": 1, "role": "owner",
                "mic": True, "speaker": True, "rate": 24000})}
        async def close(self, **reason):
            self.closed.append(reason)

    hub = LiveCallHub(cp.CallSpec("synthetic-role", 1, "synthetic", "synthetic"))
    socket = Socket()
    asyncio.run(serve_peer(socket, hub, role="meeting", skill="synthetic-transport"))
    assert socket.closed[0] == {"code": 1008, "reason": "peer_role_forbidden"}
    assert not hub.peers() and not hub.ended_by_owner
    hub.close("fixture_cleanup")


def test_presence_utterance_identity_and_observed_speaker_survive(tmp_path):
    from ouroboros.dialogue_provenance import run_origin
    from ouroboros.presence_call import record_call_utterance
    from ouroboros.presence_runner import presence_chat_id
    from tests._dialogue import chat_rows

    key = "synthetic:account:room:thread"
    task = {"id": "synthetic-presence-call", "chat_id": presence_chat_id(key), "metadata": {"presence": {
        "binding_id": "synthetic-binding", "transport_skill": "synthetic-transport", "behavior_skill": "synthetic-behavior",
        "event": {"source_event_id": "meeting:join", "provider": "synthetic", "account_id": "account",
                  "conversation_id": "room", "thread_id": "thread", "conversation_key": key,
                  "actor": {"platform_actor_id": "meeting", "display_name": "Synthetic meeting"},
                  "conversation": {}, "message": {}}}}}
    for _ in range(2):
        assert record_call_utterance(tmp_path, task, direction="in", text="Participant words.",
                                     speaker_label="Dana", utterance_id="one")
    assert record_call_utterance(tmp_path, task, direction="out", text="Generated reply.", utterance_id="two")
    rows = chat_rows(tmp_path)
    assert len(rows) == 2
    assert rows[0]["client_message_id"] == "meeting:join:heard:one"
    assert rows[1]["client_message_id"] == "meeting:join:spoken:two"
    assert rows[0]["sender_label"] == "Dana"
    assert rows[0]["transport"]["actor"]["display_name"] == "Dana"
    assert {row["presence_provenance"]["binding_id"] for row in rows} == {"synthetic-binding"}
    assert {row["chat_id"] for row in rows} == {task["chat_id"]}
    assert run_origin(task)["owner_ingress"] is False


def test_call_event_uses_ordinary_presence_binding_and_history_identity():
    from types import SimpleNamespace
    from ouroboros.presence_runner import PresenceTurnError, presence_chat_id, presence_turn_event, _stable_numeric_id
    from ouroboros.presence_bindings import conversation_key

    admission = SimpleNamespace(origin=SimpleNamespace(
        transport="synthetic", account_id="account", conversation_id="room", thread_id="thread"))
    payload = dict(source_event_id="event", provider="synthetic", account_id="account",
        conversation_id="room", thread_id="thread", conversation_key="owner-forged-room",
        actor={"platform_actor_id": "participant"}, conversation={}, message={}, text="Participant words")
    event = presence_turn_event(payload, admission)
    assert event.conversation_key == conversation_key("synthetic", "account", "room", "thread")
    assert presence_chat_id(event.conversation_key) == _stable_numeric_id("presence-conversation", event.conversation_key)
    for field in ("provider", "account_id", "conversation_id", "thread_id"):
        with pytest.raises(PresenceTurnError, match="presence_origin_mismatch"):
            presence_turn_event({**payload, field: "forged"}, admission)
    with pytest.raises(PresenceTurnError, match="presence_identity_missing"):
        presence_turn_event({**payload, "actor": {}}, admission)
