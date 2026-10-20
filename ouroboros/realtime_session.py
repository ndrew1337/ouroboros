"""Mechanical JSON websocket lifetime shared by in-process provider packages.

No provider grammar, context selection, resumption policy, pricing or credential
lookup lives here. The host leases this entire object through close, including
a connection that was interrupted before it finished opening.
"""
from __future__ import annotations

import asyncio
import json

from ouroboros.contracts.realtime import RealtimeSetupError, canonical_json


async def joined(awaitable):
    """Join owned work even when repeated Stop requests interrupt the waiter."""
    task = asyncio.ensure_future(awaitable)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class JsonSession:
    # Providers may hold native transcription/boundary work after generation
    # stops. The feature waits for that work before replacing a connection.
    renewal_ready = True

    def __init__(self, selected, options, *, connect, observe):
        self.selected, self.options = selected, options
        self.connect, self.observe = connect, observe
        self.ws = self.connection = None
        self._send_lock = asyncio.Lock()

    async def connect_wire(self):
        self.connection = self.connect(self.selected.model)
        self.ws = await self.connection.__aenter__()

    async def write(self, payload):
        raw = canonical_json(payload)
        async with self._send_lock:
            self.observe("send", raw)
            await self.ws.send(raw)

    async def read(self):
        raw = await self.ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        self.observe("receive", raw)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise RealtimeSetupError("wire", "provider event must be an object")
        if "error" in value:
            # Raw evidence is retained in the task. Provider text may echo credentials;
            # it never crosses the owner UI as a formatted exception.
            raise RealtimeSetupError("provider_error", "provider refused a live event; see the task evidence")
        return value

    def final_captions(self):
        """Known but unfinished text at actual teardown; no new wire work."""
        return []

    async def close(self):
        connection, self.connection = self.connection, None
        if connection is not None:
            await joined(connection.__aexit__(None, None, None))
        self.ws = None
