"""Physical provider uplink accounting; images spend only microphone room.

TCP is ordered: a complete image cannot be preempted by a subsequent microphone
message. Fence each image with a pong before admitting another. Bound its bytes
by the existing audio-buffer time; pongs pace delivery behind existing microphone bytes.
Nothing here inspects image meaning. The source bytes remain with Attention.
"""
from __future__ import annotations

import asyncio
import base64
import io
import time
from collections import OrderedDict, deque
from dataclasses import replace

from ouroboros.call_peers import AUDIO_QUEUE_FRAMES, MIX_FRAME_MS

AUDIO_ROOM_S = AUDIO_QUEUE_FRAMES * MIX_FRAME_MS / 1000
# Leave half of the existing capture-buffer time for the next microphone write
# and scheduling jitter. This bounds TCP head-of-line occupancy, not attention.
FRAME_DRAIN_S = AUDIO_ROOM_S / 2
SLOW = 'The screen reaches you only now and then while the upload is slow. The microphone goes first; a waiting frame is not a frame you have seen.'
RECOVERED = 'The upload has room for full shared frames again.'


DEFERRED = ('These pixels have not been delivered. Live frames wait for upload room, with newer frames '
            'replacing older ones. This exact file remains available to the image reader.')


def deferred_part(part):
    return {'type': 'text', 'text': DEFERRED + (f" File: {part['_source_path']}" if part.get('_source_path') else '')}


def replay_frames(rows):
    """A new socket has no frame throughput sample yet; replay readers, never unpriced pixels.

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
    if limit is not None and len(frame.data) <= limit:
        return frame
    if limit is not None and limit <= 0:
        return None
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(frame.data)) as source:
        image = ImageOps.exif_transpose(source).convert('RGB')
        image.info.clear()  # delivery pixels only; original metadata stays in the retained source
        side = 8 if limit is None else max(image.size)
        while side >= 8:
            copy = image.copy()
            copy.thumbnail((side, side))
            for quality in ((40,) if limit is None else (70, 55, 40)):  # the page's existing physical payload ladder
                output = io.BytesIO()
                copy.save(output, format='JPEG', quality=quality)
                data = output.getvalue()
                if limit is None or len(data) <= limit:
                    return replace(frame, data=data, mime='image/jpeg')
            side //= 2
    return None


class Upload:
    def __init__(self, session):
        self.session = session
        self.rate = None
        self.audio_rate = 0.
        self.audio_packet_s = MIX_FRAME_MS / 1000
        self.rtt = 0.
        self.slot = None
        self.generations = {}
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
        self.ready = asyncio.Event()

    async def setup(self):
        """An unloaded round trip; setup writes never supply a bandwidth sample."""
        ping = getattr(self.session.ws, 'ping', None)
        if ping is not None:
            started = time.monotonic()
            await (await ping())
            self.rtt = time.monotonic() - started

    def busy(self):
        return self.slot is not None or (self.meter is not None and not self.meter.done())

    def fence(self, count, started):
        # A successful write keeps its custody even if sending/awaiting the ping
        # subsequently fails. This task owns that receipt independently of senders.
        self.meter = asyncio.create_task(self.measure(count, started))

    async def measure(self, count, started, pong=None):
        try:
            ping = getattr(self.session.ws, 'ping', None)
            if pong is None and ping is not None:
                pong = await ping()
            if pong is not None:
                await pong
                elapsed = time.monotonic() - started
                if self.rtt < elapsed:
                    elapsed -= self.rtt
                if elapsed > 0:  # an equal clock tick / stale RTT is not infinite throughput
                    self.rate = count / elapsed
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc
        finally:
            self.ready.set()

    def capacity(self):
        from ouroboros.call_protocol import MAX_VIDEO_FRAME_PAYLOAD
        return min((self.rate or 0) * FRAME_DRAIN_S, MAX_VIDEO_FRAME_PAYLOAD * 4 / 3)

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
        self.generations[source] = self.generations.get(source, 0) + 1
        for key, claims in list(self.deliveries.items()):
            kept = deque(c for c in claims if c[0].source != source)
            if kept:
                self.deliveries[key] = kept
            else:
                del self.deliveries[key]
        if self.slot is not None and self.slot.source == source:
            self.slot = None
        self.notice(False, source)

    async def admit(self, frame):
        """One resize and admission owner for streams and requested looks."""
        if self.busy():
            return None
        generation = self.generations.get(frame.source, 0)
        limit = None if self.rate is None else self.session.frame_byte_limit(frame, int(self.capacity()))
        if self.rate is not None and limit < len(frame.data):
            self.notice(True, frame.source)
        cached = self.copies.get(frame.source)
        if cached is not None and cached[0] is frame and cached[1] == limit:
            copy = cached[2]  # None is a cached result too
        else:
            from ouroboros.realtime_session import joined
            copy = await joined(asyncio.to_thread(smaller_frame, frame, limit))
            self.copies[frame.source] = (frame, limit, copy)
        if (copy is None or self.busy() or self.audio_waiters or self.session._send_lock.locked()
                or self.generations.get(frame.source, 0) != generation
                or self.rate is not None and limit <= 0):
            return None
        self.slot = frame
        return copy

    def completed(self, frame, copy, was_reduced=False, generation=None):
        # A completed websocket write is delivery custody even if drain evidence
        # disappears afterwards. Never silently spend the same source bytes twice.
        if self.rate is not None and (generation is None or generation == self.generations.get(frame.source, 0)):
            self.notice(was_reduced or copy is not frame, frame.source)
        if self.slot is frame:
            self.slot = None
        self.ready.set()

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
                    self.deliveries.setdefault(encoded, deque()).append((frame, copy, part.get('_upload_reduced', False), self.generations.get(frame.source, 0)))
                    parts.append({**part, '_upload_reduced': copy is not frame or part.get('_upload_reduced', False),
                                  'image_url': {'url': 'data:' + copy.mime + ';base64,' + encoded}})
            output.append({**row, 'content': parts, **({'_deferred_frame': True} if held else {})})
        return output

    def written(self, payload, claims=None):
        """Recognize the exact admitted pixels after the adapter's actual write.

        Gemini carries base64 data; OpenAI carries a data URL. Traversal is by
        value, never a substring in provider text. Failed writes keep the claim.
        """
        if not self.deliveries:
            return
        if isinstance(payload, dict):
            for value in payload.values():
                self.written(value, claims)
        elif isinstance(payload, list):
            for value in payload:
                self.written(value, claims)
        elif isinstance(payload, str):
            key = payload.split(';base64,', 1)[-1] if payload.startswith('data:') else payload
            deliveries = self.deliveries.get(key)
            if deliveries and (claims is None or any(deliveries[0] is claim for claim in claims.get(key, ()))):
                self.completed(*deliveries.popleft())
                if not deliveries:
                    self.deliveries.pop(key)

    async def wait_room(self):
        self.ready.clear()
        timer = asyncio.get_running_loop().call_later(self.audio_packet_s, self.ready.set)
        try:
            await self.ready.wait()
        finally:
            timer.cancel()  # bounded admission retries also work while the microphone is muted

    async def frames(self):
        try:
            while self.pending:
                source, (frame, done, valid) = next(iter(self.pending.items()))
                if not valid():
                    self.pending.pop(source)
                    continue
                generation = self.generations.get(source, 0)
                prepared = await self.admit(frame)
                if self.pending.get(source, (None,))[0] is not frame:
                    if self.slot is frame:
                        self.slot = None
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
                    if valid() and generation == self.generations.get(source, 0):
                        done()
                    self.completed(frame, prepared, generation=generation)
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
        self.deliveries.clear()
        self.copies.clear()
        self.slot = None
