"""Mechanical JSON websocket lifetime shared by in-process provider packages.

No provider grammar, context selection, resumption policy, pricing or credential
lookup lives here. The host leases this entire object through close, including
a connection that was interrupted before it finished opening.
"""
from __future__ import annotations

import asyncio
import json
import string

from ouroboros.contracts.realtime import RealtimeSetupError, canonical_json


def connect_provider(uri, *, headers):
    """Share flow control for data, keepalive and automatic control frames.

    The asyncio connection owns one drain waiter per writer and removes it on
    cancellation. Legacy's single waiter races with ping/pong even when our JSON
    sends are locked. Keep native heartbeat detection and the prior direct route.
    """
    from websockets.asyncio.client import connect

    # A dead-socket bound for the opening handshake, not a timing judgment: on 28.10 a loaded Mac
    # needed more than 10 s and a call failed to start (owner 3A: wait up to 30 s).
    return connect(uri, additional_headers=headers, proxy=None,
                   open_timeout=30, close_timeout=3, max_size=None,
                   ping_interval=20, ping_timeout=20)


async def joined(awaitable):
    """Settle owned work, then propagate cancellation of its waiter."""
    task = asyncio.ensure_future(awaitable)
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
    result = task.result()
    if cancelled is not None:
        raise cancelled
    return result


# A refused websocket handshake, typed for the owner: a wrong or revoked key, a quota, an outage.
_HANDSHAKE_REFUSALS = {401: "authentication", 403: "authentication", 429: "rate_limit"}


def transport_loss(error):
    """Wire termination, not a provider refusal. RFC 6455 / IANA close codes.

    No received close frame means the network/heartbeat failed. 1011 alone is
    ambiguous (Google also uses it for depleted credit), so only its exact
    neutral internal-error/expiry sentences qualify; never match keywords in
    arbitrary provider text. Error events and failed responses stay typed.
    """
    received = getattr(error, "rcvd", None)
    if received is None:
        return True
    return received.code in {1000, 1001, 1005, 1006, 1012, 1013, 1014} or (
        received.code == 1011 and (not received.reason.strip()
            or received.reason.strip().rstrip(string.punctuation).casefold() in {
                "internal error encountered", "deadline expired", "deadline expired before operation could complete"}))


class JsonSession:
    # Providers may hold native transcription/boundary work after generation
    # stops. The feature waits for that work before replacing a connection.
    renewal_ready = True

    def __init__(self, selected, options, *, connect, observe):
        self.selected, self.options = selected, options
        self.connect, self.observe = connect, observe
        self.ws = self.connection = None
        self._send_lock = asyncio.Lock()
        from ouroboros.live_upload import Upload
        self.upload = Upload(self)

    async def connect_wire(self):
        self.connection = self.connect(self.selected.model)
        try:
            self.ws = await self.connection.__aenter__()
            self.upload.bind()
        except Exception as exc:  # noqa: BLE001 - only the handshake status is read; anything else keeps its type
            status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
            code = _HANDSHAKE_REFUSALS.get(status) or ("engine_unavailable" if type(status) is int and status >= 500 else None)
            if code is None:
                raise
            raise RealtimeSetupError(code, f"the voice service answered the connection with HTTP {status}") from exc

    async def write(self, payload, *, image=False, audio=False):
        raw = canonical_json(payload)
        if self.upload.error is not None:
            raise self.upload.error
        async def send():
            if image:
                from ouroboros.live_upload import socket_backlog
                while socket_backlog(self.ws) or self.upload.audio_waiters:
                    await self.upload.wait_room()
            self.observe("send", raw)
            # Setup establishes the line; images refresh that observation. Small
            # control/PCM messages are application-limited, not bandwidth probes.
            self.upload.note(self.wire_bytes(raw), calibrate=not audio and (image or self.upload.rate is None))
            claims = {key: tuple(values) for key, values in self.upload.deliveries.items()}
            await self.ws.send(raw)
            if asyncio.current_task() is not self.upload.frame_task:
                self.upload.written(payload, claims)
        if image or asyncio.current_task() is self.upload.frame_task:
            # Native asyncio websockets supports concurrent unfragmented sends.
            # A bounded image must never own the microphone's application lock.
            await send()
        else:
            async with self._send_lock:
                await send()

    def wire_bytes(self, raw):
        # Conservative pre-encoding envelope price; negotiated compression can
        # save bytes. The socket meter observes actual transport bytes separately.
        transport = getattr(self.ws, 'transport', None)
        tls = transport and transport.get_extra_info('ssl_object')
        size = len(raw.encode('utf-8'))
        size += 6 if size < 126 else 8 if size < 65536 else 14
        return size + (((size + 16383) // 16384) * (22 if tls.version() == 'TLSv1.3' else 37) if tls else 0)

    async def write_audio(self, payload, command):
        seconds = len(command.pcm) / (command.rate * 2)
        if seconds:
            self.upload.audio_packet_s = seconds
            self.upload.audio_rate = self.wire_bytes(canonical_json(payload)) / seconds
        from ouroboros.live_upload import AUDIO_WRITE
        token = AUDIO_WRITE.set({'upload': self.upload, 'seconds': seconds, 'bytes': 0} if seconds else None)
        self.upload.audio_waiters += 1
        try:
            await self.write(payload, audio=True)
        finally:
            AUDIO_WRITE.reset(token)
            self.upload.audio_waiters -= 1
            self.upload.ready.set()
        await asyncio.sleep(0)  # let a ready, bounded image use the just-drained slot

    def frame_byte_limit(self, frame, budget):
        from dataclasses import replace
        empty = replace(frame, data=b'')
        overhead = self.wire_bytes(canonical_json(self.video_payload(empty)))
        return max(0, (budget - overhead) // 4 * 3)

    def offer_video(self, frame, done, valid=lambda: True):
        if not callable(getattr(self, 'video_payload', None)):
            return False  # an owner-edited older adapter keeps its original send contract
        self.upload.offer(frame, done, valid)
        return True

    async def read(self):
        if self.upload.error is not None:
            raise self.upload.error
        raw = await self.ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        self.observe("receive", raw)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise RealtimeSetupError("wire", "provider event must be an object")
        if "error" in value and not self.is_benign_error(value):
            # Raw evidence is retained in the task. Provider text may echo credentials;
            # it never crosses the owner UI as a formatted exception.
            raise RealtimeSetupError("provider_error", "provider refused a live event; see the task evidence")
        return value

    def is_benign_error(self, event):
        """Only a provider can recognize an expected error for its own request."""
        return False

    def final_captions(self):
        """Known but unfinished text at actual teardown; no new wire work."""
        return []

    async def close(self):
        connection, self.connection = self.connection, None
        try:
            await self.upload.close()
            if connection is not None:
                await joined(connection.__aexit__(None, None, None))
        finally:
            self.ws = None
