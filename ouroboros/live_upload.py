"""Physical provider uplink accounting; images spend only spare microphone capacity.

TCP is ordered: a complete image cannot be preempted by a subsequent microphone
message. Keep one image write in flight, gate it on an empty socket, and bound its
wire bytes by the existing audio-buffer time; pacing spends measured spare throughput.
Nothing here inspects image meaning. The source bytes remain with Attention.
"""
from __future__ import annotations

import asyncio
import base64
import io
import socket
import select
import struct
import sys
import time
from collections import OrderedDict, deque
from contextvars import ContextVar
from dataclasses import replace

from ouroboros.call_peers import AUDIO_QUEUE_FRAMES, MIX_FRAME_MS
from ouroboros.contracts.realtime import canonical_json

AUDIO_WRITE = ContextVar('live_audio_write', default=None)

AUDIO_ROOM_S = AUDIO_QUEUE_FRAMES * MIX_FRAME_MS / 1000
# Leave half of the existing capture-buffer time for the next microphone write
# and scheduling jitter. This bounds TCP head-of-line occupancy, not attention.
FRAME_DRAIN_S = AUDIO_ROOM_S / 2
# The admission tick also bounds drainage polling; no sub-millisecond busy wait.
DRAIN_POLL_S = MIX_FRAME_MS / 1000
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

    def writelines(self, lines):
        self.write(b"".join(lines))

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
        except (OSError, AttributeError, ValueError, NotImplementedError):
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
        except (OSError, AttributeError, ValueError, NotImplementedError):
            pass
        return None

    def acknowledged(self):
        queued = self.tcp()
        if queued is None:
            return None
        native = buffer_size(self.native)
        return max(0, self.written - queued - native) if native is not None else None

    def rtt(self):
        sock = self.native.get_extra_info('socket')
        try:
            if sys.platform == 'darwin':
                return struct.unpack_from('=I', sock.getsockopt(socket.IPPROTO_TCP, 0x106, 104), 44)[0] / 1000
            if sys.platform.startswith('linux'):
                return struct.unpack_from('=I', sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 104), 68)[0] / 1_000_000
        except (OSError, AttributeError, ValueError, NotImplementedError):
            pass
        return 0.


def bind_meter(ws, upload=None):
    native = getattr(ws, 'transport', None)
    if native is None:
        return None
    # Count below TLS, where written bytes and the TCP queue share the same
    # unit. Verified against CPython 3.10 asyncio.sslproto; this private seam is
    # optional. The public SSL transport and its flow control remain in place.
    ssl_protocol = getattr(native, '_ssl_protocol', None)
    below = getattr(ssl_protocol, '_transport', None)
    meter = WireMeter(below or native, upload)
    if below is not None:
        ssl_protocol._transport = meter
    else:
        ws.transport = meter
    ws._upload_meter = meter
    return meter


def buffer_size(transport):
    try:
        return transport.get_write_buffer_size()
    except (AttributeError, NotImplementedError, OSError, ValueError):
        return None  # some opaque transports inherit asyncio's abstract accessor


def socket_backlog(ws):
    meter = getattr(ws, '_upload_meter', None)
    measured = meter.unsent() if meter is not None else None
    if measured is not None:
        return measured
    # Windows, opaque/proxied sockets and partial ACK-only implementations still
    # expose bytes not handed to the kernel. Do not fence the ever-moving audio tail.
    transport = meter.native if meter is not None else getattr(ws, 'transport', None)
    return buffer_size(transport)


DEFERRED = ('These pixels have not been delivered. Live frames wait for upload room, with newer frames '
            'replacing older ones. This exact file remains available to the image reader.')


def deferred_part(part):
    return {'type': 'text', 'text': DEFERRED + (f" File: {part['_source_path']}" if part.get('_source_path') else '')}


def replay_frames(rows):
    """A new socket has no measured credit yet; replay readers, never unpriced pixels.

    Only requested live looks carry this mark. Originals stay canonical; ordinary
    attachments are unaffected. Live source delivery resumes once the socket is measured.
    """
    return [{**row, 'content': [deferred_part(p) if p.get('type') == 'image_url' else p
                              for p in row['content']], '_deferred_frame': True}
            if row.get('_call_frame_source') and isinstance(row.get('content'), list) else row for row in rows]


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
        self.slow_sources = set()
        self.copies = {}
        self.deliveries = {}
        self.notices = []
        self.frame_task = None
        self.audio_waiters = 0
        self.credit = 0.
        self.credit_initialized = False
        self.credit_at = time.monotonic()
        self.total_bytes = self.ack_bytes = self.fence_bytes = 0
        self.socket_meter = None
        self.ack_start = 0
        self.ready = asyncio.Event()

    def backlog(self):
        native = socket_backlog(self.session.ws)
        if native is not None:
            return native
        # Last fallback for transports exposing no buffer: fence the bounded
        # calibration write, not the ever-moving microphone tail.
        return 0 if self.ack_bytes and self.ack_bytes >= self.fence_bytes else None

    def bind(self):
        self.socket_meter = bind_meter(self.session.ws, self)

    def progress(self):
        wire = self.socket_meter
        if wire is None:
            return None
        acknowledged = wire.acknowledged()
        if acknowledged is not None:
            return acknowledged
        native = getattr(wire, 'native', None)
        size = buffer_size(native)
        return max(0, wire.written - size) if size is not None else None

    def measured_rate(self, rate):
        # If ongoing PCM leaves the queue empty, a sample slower than that PCM
        # demand is application/scheduling limited, not a smaller link capacity.
        # It cannot revoke the earlier drain witness and strand all later frames.
        if rate > 0 and not (rate < self.audio_rate and self.backlog() == 0):
            self.rate = rate

    def note(self, count, *, calibrate=False):
        self.total_bytes += count
        # Microphone packets are application-limited. Their ACK latency cannot
        # measure spare upload capacity; nor can a control smaller than one
        # microphone packet. Setup and bounded frames supply real drain samples.
        if self.socket_meter is not None and (not calibrate or
                self.rate is not None and count <= self.audio_rate * self.audio_packet_s):
            return
        if not calibrate and self.rate is not None:
            return
        if calibrate or not self.audio_waiters:
            self.fence_bytes = self.total_bytes
        if self.started is None:
            self.started = time.monotonic()
            self.bytes = 0
            self.ack_start = self.progress()
        self.bytes += count
        if self.meter is None or self.meter.done():
            self.meter = asyncio.create_task(self.measure())

    async def measure(self):
        audio_context = AUDIO_WRITE.set(None)
        try:
            await asyncio.sleep(0)
            wire = self.socket_meter
            logical_marker = self.total_bytes
            if wire is not None and self.ack_start is not None:
                # Fence this batch, not the entire ever-moving microphone stream.
                marker = wire.written
                while True:
                    acknowledged = self.progress()
                    if acknowledged is None:
                        break  # fall back to one fixed pong fence below
                    if acknowledged >= marker:
                        break
                    # Resolve a fast setup/frame within one capture packet;
                    # a longer stall polls only at the ordinary admission tick.
                    delay = DRAIN_POLL_S / AUDIO_QUEUE_FRAMES if time.monotonic() - self.started < DRAIN_POLL_S else DRAIN_POLL_S
                    await asyncio.sleep(delay)
                elapsed = time.monotonic() - self.started
                rtt = wire.rtt()
                # The ACK fence includes propagation, not just drainage. Remove
                # the socket's measured RTT when it fits this sample; a stale
                # longer RTT must not manufacture infinite throughput.
                elapsed -= rtt if rtt < elapsed else 0
                # The poll can observe more than the fixed fence (ongoing audio).
                # Price all observed progress over that interval, not an older byte count.
                rate = (acknowledged - self.ack_start) / elapsed if acknowledged is not None else 0
                if acknowledged is not None:
                    self.ack_bytes = logical_marker
                    self.measured_rate(rate)
                    return
            ping = getattr(self.session.ws, 'ping', None)
            if ping is not None:
                count, marker = self.bytes, self.total_bytes
                pong = await ping()
                await pong
                self.ack_bytes = marker
                self.measured_rate(count / (time.monotonic() - self.started))
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

    def notice(self, slow, source=''):
        if slow:
            self.slow_sources.add(source)
        else:
            self.slow_sources.discard(source)
        current = bool(self.slow_sources)
        if current != self.slow:
            self.slow = current
            self.notices.append(SLOW if current else RECOVERED)

    def forget(self, source):
        self.pending.pop(source, None)
        self.copies.pop(source, None)
        self.notice(False, source)

    async def admit(self, frame):
        """One resize, admission and credit owner for streams and requested looks."""
        limit = self.session.frame_byte_limit(frame, int(self.capacity()))
        if self.rate is not None and limit < len(frame.data):
            self.notice(True, frame.source)
        cached = self.copies.get(frame.source)
        if cached is not None and cached[0] is frame and cached[1] == limit:
            copy = cached[2]  # None is a cached result too
        else:
            from ouroboros.realtime_session import joined
            copy = await joined(asyncio.to_thread(smaller_frame, frame, limit))
            self.copies[frame.source] = (frame, limit, copy)
        if (copy is None or self.audio_waiters or self.session._send_lock.locked() or self.backlog() != 0
                or len(copy.data) > self.session.frame_byte_limit(copy, self.budget())):
            return None
        self.credit = max(0, self.credit - self.session.wire_bytes(canonical_json(self.session.video_payload(copy))))
        return copy

    def completed(self, frame, copy, was_reduced=False):
        # A completed websocket write is delivery custody even if drain evidence
        # disappears afterwards. Never silently spend the same source bytes twice.
        self.notice(was_reduced or copy is not frame, frame.source)

    async def image_rows(self, rows, deferred):
        output = []
        from ouroboros.contracts.live_conversation import Video
        for row in rows:
            if not row.get('_call_frame_source'):
                output.append(row)
                continue
            parts, held = [], False
            for part in row.get('content', []):
                if part.get('type') != 'image_url':
                    parts.append(part)
                    continue
                prefix, encoded = part['image_url']['url'].split(',', 1)
                frame = Video(base64.b64decode(encoded), prefix[5:].split(';')[0], row['_call_frame_source'])
                copy = await self.admit(frame)
                if copy is None:
                    held = True
                    deferred(frame.source)
                    parts.append(deferred_part(part))
                else:
                    encoded = base64.b64encode(copy.data).decode()
                    self.deliveries.setdefault(encoded, deque()).append((frame, copy, part.get('_upload_reduced', False)))
                    parts.append({**part, '_upload_reduced': copy is not frame or part.get('_upload_reduced', False),
                                  'image_url': {'url': 'data:' + copy.mime + ';base64,' + encoded}})
            output.append({**row, 'content': parts, **({'_deferred_frame': True} if held else {})})
        return output

    def written(self, payload):
        """Recognize the exact admitted pixels after the adapter's actual write.

        Gemini carries base64 data; OpenAI carries a data URL. Traversal is by
        value, never a substring in provider text. Failed writes keep the claim.
        """
        if not self.deliveries:
            return
        if isinstance(payload, dict):
            for value in payload.values():
                self.written(value)
        elif isinstance(payload, list):
            for value in payload:
                self.written(value)
        elif isinstance(payload, str):
            key = payload.split(';base64,', 1)[-1] if payload.startswith('data:') else payload
            deliveries = self.deliveries.get(key)
            if deliveries:
                self.completed(*deliveries.popleft())
                if not deliveries:
                    self.deliveries.pop(key)

    async def wait_room(self):
        self.ready.clear()
        timer = asyncio.get_running_loop().call_later(self.audio_packet_s, self.ready.set)
        try:
            await self.ready.wait()
        finally:
            timer.cancel()  # physical credit also replenishes while the microphone is muted

    async def frames(self):
        try:
            while self.pending:
                source, (frame, done, valid) = next(iter(self.pending.items()))
                if not valid():
                    self.pending.pop(source)
                    continue
                prepared = await self.admit(frame)
                if self.pending.get(source, (None,))[0] is not frame:
                    continue
                if prepared is None:
                    cached = self.copies.get(source)
                    if cached is not None and cached[0] is frame and cached[2] is None:
                        self.pending.move_to_end(source)  # an impossible image cannot starve another source
                    await self.wait_room()
                    continue
                self.pending.pop(source)
                self.inflight[source] = frame
                self.frame_task = asyncio.current_task()
                try:
                    await self.session.send(prepared)
                    done()
                    self.completed(frame, prepared)
                    # Admission still waits for the preceding image to leave the
                    # observable queue; loss of measurement cannot undo its write.
                    while True:
                        backlog = self.backlog()
                        if backlog in (0, None):
                            break
                        await asyncio.sleep(DRAIN_POLL_S)
                finally:
                    self.inflight.pop(source, None)
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
