"""Physical provider uplink accounting; images spend only spare microphone capacity.

TCP is ordered: a complete image cannot be preempted by a subsequent microphone
message. Keep one image write in flight, gate it on an empty socket, and bound its
wire bytes by the existing audio-buffer time; pacing spends measured spare throughput.
Nothing here inspects image meaning. The source bytes remain with Attention.
"""
from __future__ import annotations

import asyncio
import io
import socket
import select
import struct
import sys
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import replace

from ouroboros.call_peers import AUDIO_QUEUE_FRAMES, MIX_FRAME_MS
from ouroboros.contracts.realtime import canonical_json

AUDIO_WRITE = ContextVar('live_audio_write', default=None)

AUDIO_ROOM_S = AUDIO_QUEUE_FRAMES * MIX_FRAME_MS / 1000
# Leave half of the existing capture-buffer time for the next microphone write
# and scheduling jitter. This bounds TCP head-of-line occupancy, not attention.
FRAME_DRAIN_S = AUDIO_ROOM_S / 2
# Poll physical drainage more finely than a capture packet, not a conversation timer.
DRAIN_POLL_S = MIX_FRAME_MS / 1000 / AUDIO_QUEUE_FRAMES
SLOW = 'The screen reaches you only now and then while the upload is slow. The microphone goes first; a waiting frame is not a frame you have seen.'
RECOVERED = 'The upload has room for full shared frames again.'


class WireMeter:
    """Count actual transport writes, including TLS records and native ping/pong.

    snd_sbbytes / TIOCOUTQ includes packets already on the wire. Admission uses
    unsent bytes; ACK progress measures throughput without requiring silence.
    """
    def __init__(self, native, upload=None):
        self.native, self.upload = native, upload
        self.written = 0
        self.empty_check = False
        sock = native.get_extra_info('socket')
        if sock is not None and sys.platform == 'darwin':
            try:
                # Public TCP_NOTSENT_LOWAT: writable when unsent bytes fall
                # below 1, i.e. empty. In-flight audio remains lawful.
                sock.setsockopt(socket.IPPROTO_TCP, 0x201, 1)
                self.empty_check = True
            except OSError:
                pass

    def __getattr__(self, name):
        return getattr(self.native, name)

    def write(self, data):
        self.written += len(data)
        audio = AUDIO_WRITE.get()
        if audio is not None and audio['upload'] is self.upload:
            audio['bytes'] += len(data)
            self.upload.audio_rate = audio['bytes'] / audio['seconds']
        self.native.write(data)

    def tcp(self):
        sock = self.native.get_extra_info('socket')
        if sock is None:
            return None
        try:
            if sys.platform == 'darwin':
                info = sock.getsockopt(socket.IPPROTO_TCP, 0x106, 104)
                return struct.unpack_from('=I', info, 32)[0]
            if sys.platform.startswith('linux'):
                import fcntl
                queued = struct.unpack('=I', fcntl.ioctl(sock.fileno(), 0x5411, struct.pack('=I', 0)))[0]
                return queued
        except (OSError, AttributeError, ValueError):
            pass
        return None

    def unsent(self):
        sock = self.native.get_extra_info('socket')
        if sock is None:
            return None
        try:
            native = self.native.get_write_buffer_size()
            if sys.platform == 'darwin' and self.empty_check:
                return native + (0 if select.select([], [sock], [], 0)[1] else 1)
            if sys.platform.startswith('linux'):
                import fcntl
                # Linux SIOCOUTQNSD: queued, but not yet sent (unlike TIOCOUTQ).
                return native + struct.unpack('=I', fcntl.ioctl(sock.fileno(), 0x894B, struct.pack('=I', 0)))[0]
        except (OSError, AttributeError, ValueError):
            pass
        return None

    def acknowledged(self):
        queued = self.tcp()
        if queued is None:
            return None
        return max(0, self.written - queued - self.native.get_write_buffer_size())

    def rtt(self):
        sock = self.native.get_extra_info('socket')
        try:
            if sys.platform == 'darwin':
                return struct.unpack_from('=I', sock.getsockopt(socket.IPPROTO_TCP, 0x106, 104), 44)[0] / 1000
            if sys.platform.startswith('linux'):
                return struct.unpack_from('=I', sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 104), 68)[0] / 1_000_000
        except (OSError, AttributeError, ValueError):
            pass
        return 0.


def bind_meter(ws, upload=None):
    native = getattr(ws, 'transport', None)
    if native is None:
        return None
    # Count below TLS, where written bytes and the TCP queue share the same
    # unit. The public SSL transport remains in place, including its flow control.
    ssl_protocol = getattr(native, '_ssl_protocol', None)
    below = getattr(ssl_protocol, '_transport', None)
    meter = WireMeter(below or native, upload)
    if below is not None:
        ssl_protocol._transport = meter
    else:
        ws.transport = meter
    ws._upload_meter = meter
    return meter


def socket_backlog(ws):
    meter = getattr(ws, '_upload_meter', None)
    return meter.unsent() if meter is not None else None


def smaller_frame(frame, limit):
    """Keep originals verbatim when possible; encode a delivery copy to a byte cap.

    JPEG's minimum spatial unit is an 8x8 DCT block. Stop at that codec bound,
    rather than inventing a content/readability threshold. Never enlarge a source.
    """
    if len(frame.data) <= limit:
        return frame
    if limit <= 0:
        return None
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(frame.data)) as source:
        image = ImageOps.exif_transpose(source).convert('RGB')
        image.info.clear()  # delivery pixels only; original metadata stays in the retained source
        side = max(image.size)
        while side >= 8:
            copy = image.copy()
            copy.thumbnail((side, side))
            for quality in (70, 55, 40):  # the page's existing physical payload ladder
                output = io.BytesIO()
                copy.save(output, format='JPEG', quality=quality)
                data = output.getvalue()
                if len(data) <= limit:
                    return replace(frame, data=data, mime='image/jpeg')
            side //= 2
    return None


class Upload:
    def __init__(self, session):
        self.session = session
        self.rate = None
        self.audio_rate = 0.
        self.audio_packet_s = MIX_FRAME_MS / 1000
        self.started = None
        self.bytes = 0
        self.meter = self.worker = None
        self.pending = OrderedDict()
        self.inflight = {}
        self.error = None
        self.slow = False
        self.notices = []
        self.writing_frame = False
        self.frame_task = None
        self.audio_waiters = 0
        self.credit = 0.
        self.credit_initialized = False
        self.credit_at = time.monotonic()
        self.total_bytes = self.ack_bytes = 0
        self.socket_meter = None
        self.ack_start = 0
        self.ready = asyncio.Event()

    def backlog(self):
        native = socket_backlog(self.session.ws)
        if native is not None:
            return native
        # Portable, conservative fallback: a websocket pong fences all preceding
        # bytes on opaque transports. Never interpret an unknown kernel queue as empty.
        return 0 if self.total_bytes and self.ack_bytes == self.total_bytes else None

    def bind(self):
        self.socket_meter = bind_meter(self.session.ws, self)

    def note(self, count, *, calibrate=False):
        self.total_bytes += count
        # Microphone packets are application-limited. Their ACK latency cannot
        # measure spare upload capacity; nor can a control smaller than one
        # microphone packet. Setup and bounded frames supply real drain samples.
        if self.socket_meter is not None and (not calibrate or
                self.rate is not None and count <= self.audio_rate * self.audio_packet_s):
            return
        if self.started is None:
            self.started = time.monotonic()
            self.bytes = 0
            self.ack_start = self.socket_meter.acknowledged() if self.socket_meter is not None else None
        self.bytes += count
        if self.meter is None or self.meter.done():
            self.meter = asyncio.create_task(self.measure())

    async def measure(self):
        audio_context = AUDIO_WRITE.set(None)
        try:
            await asyncio.sleep(0)
            wire = self.socket_meter
            if wire is not None and self.ack_start is not None:
                # Fence this batch, not the entire ever-moving microphone stream.
                marker = wire.written
                while wire.acknowledged() is not None and wire.acknowledged() < marker:
                    await asyncio.sleep(DRAIN_POLL_S)
                elapsed = time.monotonic() - self.started
                rtt = wire.rtt()
                # The ACK fence includes propagation, not just drainage. Remove
                # the socket's measured RTT when it fits this sample; a stale
                # longer RTT must not manufacture infinite throughput.
                elapsed -= rtt if rtt < elapsed else 0
                rate = (marker - self.ack_start) / max(elapsed, DRAIN_POLL_S)
                if rate > 0:
                    self.rate = rate
                return
            ping = getattr(self.session.ws, 'ping', None)
            if ping is not None:
                count, marker = self.bytes, self.total_bytes
                pong = await ping()
                await pong
                rate = count / max(time.monotonic() - self.started, DRAIN_POLL_S)
                if self.rate is None or rate > self.rate:
                    self.rate = rate
                self.ack_bytes = marker
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc
        finally:
            AUDIO_WRITE.reset(audio_context)
            self.started = None
            self.ready.set()

    def capacity(self):
        from ouroboros.call_protocol import MAX_VIDEO_FRAME_PAYLOAD
        return min((self.rate or 0) * FRAME_DRAIN_S, MAX_VIDEO_FRAME_PAYLOAD * 4 / 3)

    def budget(self):
        now = time.monotonic()
        elapsed, self.credit_at = now - self.credit_at, now
        if self.rate is None:
            return 0
        cap = self.capacity()
        spare = max(0, self.rate - self.audio_rate)
        if not self.credit_initialized:
            self.credit = cap  # one bounded first frame; subsequent frames repay that wire occupancy
            self.credit_initialized = True
        self.credit = min(cap, self.credit + elapsed * spare)
        # Accumulate spare wire capacity for one bounded image. Spending every
        # tiny audio gap on another tiny image would flood the line instead of
        # leaving room for speech. Latest/source replaces while credit rebuilds.
        return int(self.credit) if spare else 0

    def offer(self, frame, done, valid):
        current = self.inflight.get(frame.source)
        if current is not None and current.data == frame.data:
            # Input-start, an offer and attend may overlap this same send. If the
            # source changed and changed back, its intermediate waiter is older too.
            self.pending.pop(frame.source, None)
            return
        self.pending[frame.source] = (frame, done, valid)
        self.ready.set()
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self.frames())

    def notice(self, slow):
        if slow != self.slow:
            self.slow = slow
            self.notices.append(SLOW if slow else RECOVERED)

    async def wait_room(self):
        self.ready.clear()
        timer = asyncio.get_running_loop().call_later(self.audio_packet_s, self.ready.set)
        try:
            await self.ready.wait()
        finally:
            timer.cancel()  # physical credit also replenishes while the microphone is muted

    async def frames(self):
        try:
            cached_frame = cached_copy = None
            while self.pending:
                source, (frame, done, valid) = next(iter(self.pending.items()))
                if not valid():
                    self.pending.pop(source)
                    continue
                # JSON envelope size is supplied by the adapter, not guessed by
                # the shared layer. Its base64 expansion is priced exactly.
                self.budget()
                limit = self.session.frame_byte_limit(frame, int(self.capacity()))
                if self.rate is not None and self.session.frame_byte_limit(frame, int(self.capacity())) < len(frame.data):
                    self.notice(True)
                if limit <= 0:
                    await self.wait_room()
                    continue
                if (cached_frame is frame and cached_copy is not None and len(cached_copy.data) <= limit
                        and (limit < len(frame.data) or cached_copy is frame)):
                    prepared = cached_copy
                else:
                    from ouroboros.realtime_session import joined
                    prepared = await joined(asyncio.to_thread(smaller_frame, frame, limit))
                    cached_frame, cached_copy = frame, prepared
                if self.pending.get(source, (None,))[0] is not frame:
                    continue  # a newer source arrived during encoding
                if prepared is None:
                    self.pending.move_to_end(source)  # one unfit source cannot starve a smaller independent one
                    await self.wait_room()
                    continue
                # A CPU encode yields to audio; recheck immediately before the
                # atomic websocket write, including its newly measured budget.
                if (self.audio_waiters or self.session._send_lock.locked()
                        or self.backlog() != 0
                        or len(prepared.data) > self.session.frame_byte_limit(prepared, self.budget())):
                    await self.wait_room()
                    continue
                self.pending.pop(source)
                self.writing_frame = True
                self.inflight[source] = frame
                self.frame_task = asyncio.current_task()
                try:
                    self.credit = max(0, self.credit - self.session.wire_bytes(
                        canonical_json(self.session.video_payload(prepared))))
                    await self.session.send(prepared)
                    # Do not admit another image until this one has really left
                    # TCP, even when ws.send returned at its high watermark.
                    while self.backlog() != 0:
                        if self.meter is not None and self.meter.done() and self.backlog() is None:
                            return  # no delivery claim after losing the transport's evidence
                        await asyncio.sleep(DRAIN_POLL_S)
                    done()
                    if prepared is frame:
                        self.notice(False)
                finally:
                    self.inflight.pop(source, None)
                    self.writing_frame = False
                    self.frame_task = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc  # the ordinary reader/audio path owns typed failure

    async def close(self):
        tasks = [t for t in (self.worker, self.meter) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
