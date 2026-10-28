"""A rate-limited provider transport. Real websocket bytes cross loopback unchanged.

Throttle below websocket framing (including setup, audio, images and ping), not
JsonSession.send. Its drain/backlog are real to the adapter; the fake provider
receives complete messages only after their bytes have traversed this link.
"""
import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager


class LimitedTransport:
    def __init__(self, connection, rate):
        self.connection, self.native, self.rate = connection, connection.transport, rate
        self.queue = deque()
        self.queued = 0
        self.worker = None
        self.paused = False

    def __getattr__(self, name):
        return getattr(self.native, name)

    def get_write_buffer_size(self):
        return self.queued + self.native.get_write_buffer_size()

    def write(self, data):
        self.queue.append(memoryview(data))
        self.queued += len(data)
        if not self.paused and self.queued > self.native.get_write_buffer_limits()[1]:
            self.paused = True
            self.connection.pause_writing()
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self.drain())

    async def drain(self):
        at = time.monotonic()
        while self.queue:
            head = self.queue[0]
            # One millisecond of line capacity; no compression or JSON shortcut.
            count = min(len(head), max(1, int(self.rate / 1000)))
            at += count / self.rate
            await asyncio.sleep(max(0, at - time.monotonic()))
            self.native.write(head[:count])
            self.queued -= count
            if self.paused and self.queued <= self.native.get_write_buffer_limits()[0]:
                self.paused = False
                self.connection.resume_writing()
            if count == len(head):
                self.queue.popleft()
            else:
                self.queue[0] = head[count:]
        if self.paused:
            self.paused = False
            self.connection.resume_writing()

    async def finish(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)


def limit_connector(monkeypatch, rate):
    from ouroboros import realtime_session
    native = realtime_session.connect_provider
    # Exercise the stated PCM/base64 bandwidth, not compression of synthetic
    # constant samples. Production keeps its negotiated compression unchanged.
    from websockets.asyncio import client
    original = client.connect
    def uncompressed(*args, **kwargs):
        return original(*args, **{**kwargs, 'compression': None})
    monkeypatch.setattr(client, 'connect', uncompressed)
    links = []
    @asynccontextmanager
    async def connect(uri, *, headers):
        async with native(uri, headers=headers) as connection:
            link = LimitedTransport(connection, rate)
            connection.transport = link
            links.append(link)
            try:
                yield connection
            finally:
                await link.finish()
                connection.transport = link.native
    monkeypatch.setattr(realtime_session, 'connect_provider', connect)
    return links
