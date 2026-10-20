"""OpenAI wire layout and initial setup consumer. No host content selection."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from ouroboros.contracts.realtime import (
    RealtimeRoute, RealtimeSetupError, RealtimeSetupResult, canonical_json, sha256,
)

# One address for both the connection and the declared route identity.
ENDPOINT = "wss://api.openai.com/v1/realtime"
ROUTE = RealtimeRoute(accounting_provider="openai_realtime", endpoint=ENDPOINT, model_prefix="openai::")

# Host bookkeeping is retained in RealtimeInput, not model text. Unknown fields
# (including name/tool_calls) refuse instead of quietly losing their semantics.
_MESSAGE_METADATA = {
    "_stable_prefix_blocks", "_host_context_kind", "_host_text_sha256",
    "_review_history_source", "_review_context_index",
}
# Session and response ``max_output_tokens`` accept an integer 1-4096 or "inf",
# the model's maximum (API reference, Realtime, read 2026-10-20). That is field
# encoding, not the model's output capability, which stays route evidence.
_OUTPUT_FIELD_MAX = 4096
# Observed at this endpoint on 2026-10-20: conversation.item.create rejected a
# 299039-character text with string_above_max_length, maximum 256000. This is
# field packaging, not a memory/token limit; preserve every character and role.
_ITEM_TEXT_MAX = 256000


def _parts(message):
    if not isinstance(message, dict) or set(message) - {"role", "content", *_MESSAGE_METADATA}:
        raise RealtimeSetupError("message_shape", "unsupported message fields")
    role = message.get("role")
    if role not in {"system", "user", "assistant"}:
        raise RealtimeSetupError("role", f"cannot preserve role {role!r} in this setup slice")
    content = message.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list) or not content:
        raise RealtimeSetupError("content", "expected nonempty text content")
    parts = []
    for part in content:
        if (not isinstance(part, dict) or set(part) - {"type", "text", "cache_control"}
                or part.get("type") != "text" or not isinstance(part.get("text"), str)):
            raise RealtimeSetupError("content", "this setup supports text only; no attachment or block may be dropped")
        # No strip(), join(), summarization, size ceiling or character slicing.
        parts.append({"type": "output_text" if role == "assistant" else "input_text", "text": part["text"]})
    return role, parts


def _tools(raw):
    if not isinstance(raw, list):
        raise RealtimeSetupError("tools", "expected the host-selected tool list")
    result = []
    for tool in raw:
        if not isinstance(tool, dict) or set(tool) - {"type", "function", "cache_control"} or tool.get("type") != "function":
            raise RealtimeSetupError("tools", "only function schemas can be mapped losslessly")
        function = tool.get("function")
        if (not isinstance(function, dict) or set(function) - {"name", "description", "parameters"}
                or not isinstance(function.get("name"), str) or not function["name"]):
            raise RealtimeSetupError("tools", "unsupported function schema fields")
        result.append({"type": "function", **function})
    return result


def layout(selected, probe=None):
    """Preserve selected roles/text, fragmenting only at observed field limits."""
    messages = json.loads(selected.messages_json)
    if not isinstance(messages, list) or not messages:
        raise RealtimeSetupError("messages", "the complete selected message list is required")
    parsed = [_parts(message) for message in messages]  # validate ALL before opening a socket
    first_role, first_parts = parsed[0]
    if first_role != "system":
        raise RealtimeSetupError("instructions", "the ordinary leading system message is required")
    tools = _tools(json.loads(selected.tools_json))
    # No automatic response can start (turn detection is off). The one requested
    # response carries the explicit probe bound; the host allowance bounds that.
    session = {
        "type": "realtime", "instructions": first_parts[0]["text"], "tools": tools,
        "truncation": "disabled", "output_modalities": ["text"],
        "max_output_tokens": "inf",
        "audio": {"input": {"turn_detection": None}},
    }
    events = [{"type": "session.update", "event_id": "setup_session", "session": session}]

    def append_item(role, parts):
        if any(len(part["text"]) > _ITEM_TEXT_MAX for part in parts):
            for part in parts:
                text = part["text"]
                for start in range(0, len(text), _ITEM_TEXT_MAX) or (0,):
                    append_item(role, [{**part, "text": text[start:start + _ITEM_TEXT_MAX]}])
            return
        index = len(events)
        events.append({
            "type": "conversation.item.create", "event_id": f"setup_event_{index}",
            "previous_item_id": events[-1]["item"]["id"] if "item" in events[-1] else "root",
            "item": {"id": f"setup_item_{index}", "type": "message", "role": role, "content": parts},
        })

    for part in first_parts[1:]:
        append_item("system", [part])
    for role, parts in parsed[1:]:
        append_item(role, parts)
    if probe is not None:
        if probe.max_output_tokens > selected.output_allowance_tokens:
            raise RealtimeSetupError("probe", "probe output exceeds the host's selected allowance")
        if probe.max_output_tokens > _OUTPUT_FIELD_MAX:
            raise RealtimeSetupError("probe", f"OpenAI max_output_tokens is an integer 1-{_OUTPUT_FIELD_MAX} or inf")
        append_item("user", [{"type": "input_text", "text": probe.text}])
        events.append({"type": "response.create", "event_id": "setup_probe_response", "response": {
            "output_modalities": ["text"], "max_output_tokens": probe.max_output_tokens,
            "tool_choice": "none",
        }})
    wire = canonical_json({"model": selected.model, "input_sha256": selected.sha256, "events": events})
    return events, wire


def _usage(response):
    """OpenAI usage in host axes; an axis it does not report stays absent (unknown)."""
    raw = response.get("usage") if isinstance(response, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    details = raw.get("input_token_details")
    values = {"prompt_tokens": raw.get("input_tokens"), "completion_tokens": raw.get("output_tokens"),
              "cached_tokens": details.get("cached_tokens") if isinstance(details, dict) else None}
    return {axis: value for axis, value in values.items() if type(value) is int and value >= 0}


async def _receive(ws, observe):
    raw = await ws.recv()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    observe("receive", raw)  # preserve even a malformed/error event before interpreting it
    event = json.loads(raw)
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise RealtimeSetupError("wire", "expected a JSON event object")
    if event.get("type") == "error":
        error = event.get("error") or {}
        raise RealtimeSetupError("provider_error", canonical_json(error))
    if event.get("type") in {"conversation.item.deleted", "conversation.item.truncated"}:
        raise RealtimeSetupError("provider_truncation", "the selected input changed")
    return event


def _check_session(effective, sent):
    for key in ("instructions", "tools", "truncation", "output_modalities", "max_output_tokens"):
        if effective.get(key) != sent[key]:
            raise RealtimeSetupError("session_mismatch", f"provider did not retain {key}")
    if effective.get("audio", {}).get("input", {}).get("turn_detection", "missing") is not None:
        raise RealtimeSetupError("session_mismatch", "automatic responses must remain disabled during setup")


def _check_item(actual, sent):
    if any(actual.get(key) != sent[key] for key in ("id", "type", "role", "content")):
        raise RealtimeSetupError("item_mismatch", f"provider did not retain {sent['id']} exactly")


async def consume(ws, events, observe):
    """Serial acknowledgements: no probe response is requested after partial setup."""
    response = None
    response_id = None
    for outgoing in events:
        raw = canonical_json(outgoing)
        observe("send", raw)
        await ws.send(raw)
        while True:
            incoming = await _receive(ws, observe)
            kind = incoming.get("type")
            if kind == "session.updated":
                _check_session(incoming.get("session", {}), events[0]["session"])
                if outgoing["type"] == "session.update":
                    break
            elif kind in {"conversation.item.added", "conversation.item.done"}:
                item = incoming.get("item", {})
                if outgoing.get("item", {}).get("id") == item.get("id"):
                    _check_item(item, outgoing["item"])
                    if incoming.get("previous_item_id") != outgoing["previous_item_id"]:
                        # The API uses null for the first item; the client spells it root.
                        if not (outgoing["previous_item_id"] == "root" and incoming.get("previous_item_id") is None):
                            raise RealtimeSetupError("item_order", "provider changed the selected order")
                    if kind == "conversation.item.done":
                        break
                elif outgoing["type"] != "response.create" or item.get("role") != "assistant":
                    raise RealtimeSetupError("unexpected_item", "unrequested conversation input during setup")
            elif kind.startswith("response."):
                if outgoing["type"] != "response.create":
                    raise RealtimeSetupError("unexpected_response", "response began before whole input was acknowledged")
                if kind == "response.created":
                    if response_id is not None:
                        raise RealtimeSetupError("response_mismatch", "more than one response began")
                    response_id = incoming.get("response", {}).get("id")
                    if not response_id:
                        raise RealtimeSetupError("response_mismatch", "response.created has no id")
                elif kind == "response.done":
                    response = incoming.get("response", {})
                    observe("usage", canonical_json(_usage(response)))  # before the checks: failed responses bill too
                    if not response_id or response.get("id") != response_id:
                        raise RealtimeSetupError("response_mismatch", "response.done does not match the requested response")
                    if response.get("status") != "completed":
                        raise RealtimeSetupError("response_failed", canonical_json(response))
                    break
    return response


def register(api):
    @asynccontextmanager
    async def connect_openai(model):
        # No reads during registration, preview or injected-transport tests.
        key = api.get_settings(["OPENAI_API_KEY"]).get("OPENAI_API_KEY")
        if not isinstance(key, str) or not key.strip():
            raise RealtimeSetupError("credential", "OpenAI Realtime requires a granted OPENAI_API_KEY")
        from websockets.legacy.client import connect

        async with connect(
            ENDPOINT + "?" + urlencode({"model": model}),
            extra_headers={"Authorization": "Bearer " + key},
            open_timeout=10, close_timeout=3, max_size=None,
        ) as ws:
            yield ws

    async def setup(selected, *, observe, execute=False, expected_wire_sha256="", probe=None, connect=None):
        events, wire = layout(selected, probe)
        digest = sha256(wire)
        if not execute:
            return RealtimeSetupResult("prepared", selected.sha256, digest, wire)
        if expected_wire_sha256 != digest:
            raise RealtimeSetupError("wire_changed", "execute requires the digest of this exact preview")
        # The supplied factory belongs to the host test/probe caller, never a tool
        # argument or a plugin-controlled alternate URL.
        async with (connect or connect_openai)(selected.model) as ws:
            response = await consume(ws, events, observe)
        return RealtimeSetupResult("response_completed" if response is not None else "setup_accepted",
                                   selected.sha256, digest, wire,
                                   canonical_json(response) if response is not None else "")

    api.register_realtime_provider("openai", setup, route=ROUTE)
