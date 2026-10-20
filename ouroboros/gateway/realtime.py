"""Authenticated owner WebSocket transport selection and exact-preview funding."""
from __future__ import annotations

from ouroboros.contracts.realtime import RealtimeSetupError
from ouroboros.realtime_turn import RealtimeTurn


def realtime_dispatch(bridge, frame, publish, metadata):
    """Prepare the callback used AFTER the ordinary canonical owner acceptance."""
    # Ingress strips owner text before recognizing commands; this check reads it the same way.
    if frame.get("force_plan") or frame.get("attachments") or str(frame.get("content", "")).strip().startswith("/"):
        raise RealtimeSetupError("transport", "realtime setup requires an ordinary text room turn")
    turn = RealtimeTurn(frame["execution_transport"], publish=publish)
    # Durable intent is part of the canonical acceptance and task metadata.
    # It never contains the funding grant or reconstructs a live capability.
    metadata["execution_transport"] = turn.request

    def accepted(text, **message):
        metadata = dict(message.get("task_metadata") or {})
        metadata["_realtime_turn"] = turn  # runtime capability, never accepted from JSON metadata
        bridge.enqueue_local_message(text, **{**message, "task_metadata": metadata})

    return accepted


def authorize_realtime(frame):
    from supervisor.active_activity import get_direct_activity_registry

    if (not isinstance(frame, dict) or set(frame) !=
            {"type", "task_id", "chat_id", "wire_sha256", "reservation_usd"}
            or type(frame["chat_id"]) is not int or not isinstance(frame["task_id"], str)):
        raise RealtimeSetupError("funding", "expected task_id, chat_id, wire_sha256 and reservation_usd only")
    entry = get_direct_activity_registry().get(frame["task_id"])
    if entry is None or entry.chat_id != frame["chat_id"]:
        raise RealtimeSetupError("not_found", "no admitted realtime actor in this room")
    turn = getattr(entry.actor, "_realtime_turn", None)
    if not isinstance(turn, RealtimeTurn):
        raise RealtimeSetupError("transport", "the selected actor is not a realtime turn")
    turn.authorize(frame["wire_sha256"], frame["reservation_usd"])
    return {"type": "realtime_authorized", "task_id": entry.activity_id, "chat_id": entry.chat_id}
