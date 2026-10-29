"""R22 opposite controls for physical delivery, independent of OS/provider labels."""
import asyncio
import base64
from types import SimpleNamespace

import pytest

from ouroboros.contracts.live_conversation import Audio, Video
from ouroboros.live_upload import WireMeter, SLOW, RECOVERED
from tests.test_live_upload_controls import Session


@pytest.mark.parametrize('kernel', ['none', 'unsupported', 'ack_only'])
def test_buffer_fallback_tracks_unsent_bytes_with_continuous_audio(monkeypatch, kernel):
    from ouroboros.live_upload import socket_backlog
    native = SimpleNamespace(get_extra_info=lambda _: None, get_write_buffer_size=lambda: queued[0])
    queued = [123]
    meter = WireMeter(native)
    if kernel == 'unsupported':
        monkeypatch.setattr(meter, 'unsent', lambda: None)
    if kernel == 'ack_only':
        monkeypatch.setattr(meter, 'acknowledged', lambda: 1)
    ws = SimpleNamespace(_upload_meter=meter)
    assert socket_backlog(ws) == 123
    queued[0] = 0
    assert socket_backlog(ws) == 0


def test_writelines_counts_every_byte():
    sent = []
    meter = WireMeter(SimpleNamespace(get_extra_info=lambda _: None, write=sent.append,
                                      writelines=lambda lines: sent.extend(lines)))
    meter.writelines([b'abc', b'defg'])
    assert meter.written == 7 and b''.join(sent) == b'abcdefg'


def test_impossible_copy_cached_by_frame_and_exact_limit(monkeypatch):
    async def scenario():
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = object()
        session.upload.rate = 1000
        calls = []
        def encode(frame, limit):
            calls.append((frame, limit))
            return None
        monkeypatch.setattr('ouroboros.live_upload.smaller_frame', encode)
        frame = Video(b'original' * 1000, 'image/jpeg', 'a')
        for _ in range(4):
            assert await session.upload.admit(frame) is None
        assert len(calls) == 1, 'one unfit frame/cap must encode only once'
        session.upload.rate *= 2
        await session.upload.admit(frame)
        await session.upload.admit(Video(frame.data, frame.mime, frame.source))
        assert len(calls) == 3, 'a changed cap or source must be measured afresh'
    asyncio.run(scenario())


@pytest.mark.parametrize('lost', [False, True])
def test_completed_write_keeps_delivery_custody_if_drain_measurement_disappears(monkeypatch, lost):
    async def scenario():
        backlog = [0]
        monkeypatch.setattr('ouroboros.live_upload.socket_backlog', lambda ws: backlog[0])
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = object()
        session.upload.rate = 100_000
        written, done = [], []
        async def send(frame):
            written.append(frame)
            if lost:
                backlog[0] = None
        session.send = send
        frame = Video(b'original', 'image/jpeg', 'a')
        session.offer_video(frame, lambda: done.append(frame))
        await asyncio.wait_for(session.upload.worker, 2)
        assert done == written == [frame], 'completed bytes remain delivered even when drainage becomes unknowable'
        await session.upload.close()
    asyncio.run(scenario())


def test_requested_delivery_recovers_only_after_successful_write(monkeypatch):
    from tests.test_live_upload import screen
    async def scenario():
        monkeypatch.setattr('ouroboros.live_upload.socket_backlog', lambda ws: 0)
        writes, fail = [], [True]
        class Socket:
            async def send(self, raw):
                if fail[0]:
                    raise OSError('synthetic broken socket')
                writes.append(raw)
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = Socket()
        upload = session.upload
        upload.rate = 10_000_000
        upload.notice(True, 'a')
        image = {'type':'image_url', 'image_url':{'url':'data:image/jpeg;base64,' + base64.b64encode(screen()).decode()}}
        rows = await upload.image_rows([{'_call_frame_source':'a', 'content':[image]}], lambda _: None)
        assert upload.notices == [SLOW]
        with pytest.raises(OSError):
            await session.write({'image': rows[0]['content'][0]['image_url']['url']}, image=True)
        assert upload.notices == [SLOW], 'an admitted image is not a completed image'
        fail[0] = False
        await session.write({'image': rows[0]['content'][0]['image_url']['url']}, image=True)
        assert upload.notices == [SLOW, RECOVERED] and len(writes) == 1
        await session.upload.close()
    asyncio.run(scenario())


def test_audio_restores_reservation_before_socket_write(monkeypatch):
    async def scenario():
        session = Session(None, None, connect=None, observe=lambda *a: None)
        class Socket:
            async def send(self, raw):
                assert session.upload.audio_rate > 0
                assert session.upload.audio_waiters == 1
        session.ws = Socket()
        assert session.upload.audio_rate == 0
        await session.write_audio({'audio':'new pcm'}, Audio(b'\0\0' * 320, 16000))
        assert session.upload.audio_rate > 0
        await session.upload.close()
    asyncio.run(scenario())


def test_drain_poll_is_capture_sized_and_reads_once_per_tick(monkeypatch):
    from ouroboros.live_upload import DRAIN_POLL_S
    from ouroboros.call_peers import MIX_FRAME_MS, AUDIO_QUEUE_FRAMES
    assert DRAIN_POLL_S == MIX_FRAME_MS / 1000
    async def scenario():
        session = Session(None, None, connect=None, observe=lambda *a: None)
        clock, calls, sleeps = [0.], [], []
        def ack():
            calls.append(clock[0])
            return 0 if clock[0] < .1 else 100
        async def sleep(delay):
            sleeps.append((clock[0], delay))
            clock[0] += delay
        session.upload.socket_meter = SimpleNamespace(written=100, acknowledged=ack, rtt=lambda: 0)
        session.upload.started, session.upload.ack_start = 0, 0
        monkeypatch.setattr('ouroboros.live_upload.time', SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr('ouroboros.live_upload.asyncio', SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError))
        await session.upload.measure()
        assert len(calls) == len([d for _,d in sleeps if d]) + 1, 'one progress read per polling turn'
        assert len(calls) <= AUDIO_QUEUE_FRAMES + 7, 'fast sampling ends after one capture packet'
        assert all(d == DRAIN_POLL_S for at,d in sleeps if at >= DRAIN_POLL_S)
    asyncio.run(scenario())


@pytest.mark.parametrize('cancel', [False, True])
def test_failed_offer_restored_before_newer_arrivals(cancel):
    from skills.live_conversation.driver import _Call
    async def scenario():
        older, newer = {'content':'already owned'}, {'content':'arrived during write'}
        class Attention:
            async def delivery_images(self, session, rows): return rows
            async def deliver(self, session, **kwargs): return []
            def include_images(self, rows): pass
        class Socket:
            async def send(self, command):
                call.pending_offers.append(newer)
                if cancel:
                    raise asyncio.CancelledError()
                raise OSError('lost write')
        call = object.__new__(_Call)
        call.pending_offers, call.session, call.attention = [older], Socket(), Attention()
        call.host = SimpleNamespace(renewal_due=lambda: False, state={})
        call.connected = asyncio.Event()
        call.connected.set()
        call.quiet = lambda: True
        with pytest.raises(asyncio.CancelledError if cancel else OSError):
            await call.offer_pending()
        assert call.pending_offers == [older, newer]
    asyncio.run(scenario())


def test_replay_projection_leaves_ordinary_images_and_original_sources_whole():
    from ouroboros.realtime_setup import rows_for_engine
    image = {'type':'image_url', 'image_url':{'url':'data:image/jpeg;base64,eA=='}, '_source_path':'exact.jpg'}
    ordinary = {'role':'user', 'content':[image]}
    look = {**ordinary, '_call_frame_source':'screen'}
    projected = rows_for_engine([ordinary, look], vision=True)
    assert projected[0] is ordinary
    assert projected[1]['content'][0]['type'] == 'text'
    assert 'exact.jpg' in projected[1]['content'][0]['text']
    assert look['content'][0] is image


def test_opaque_fixed_fence_progress_does_not_chase_audio_tail():
    async def scenario():
        pongs = []
        class Socket:
            async def ping(self):
                future = asyncio.get_running_loop().create_future()
                pongs.append(future)
                return future
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = Socket()
        upload = session.upload
        upload.note(4000, calibrate=True)
        while not pongs:
            await asyncio.sleep(0)
        upload.audio_waiters = 1
        upload.note(100, calibrate=False)
        assert upload.backlog() is None
        pongs[0].set_result(None)
        await upload.meter
        assert upload.backlog() == 0, 'a completed bounded fence must progress despite later audio'
        upload.audio_waiters = 0
        upload.note(2000, calibrate=True)
        assert upload.backlog() is None, 'the next bounded image needs its own completion witness'
        await upload.close()
    asyncio.run(scenario())


def test_equal_pixels_keep_independent_source_completion(monkeypatch):
    from tests._live_stack_page import jpeg
    async def scenario():
        monkeypatch.setattr('ouroboros.live_upload.socket_backlog', lambda ws: 0)
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = object()
        session.upload.rate = 10_000_000
        data = base64.b64encode(jpeg()).decode()
        rows = [{'_call_frame_source':name, 'content':[{'type':'image_url', 'image_url':{'url':'data:image/jpeg;base64,'+data}}]}
                for name in ('camera', 'screen')]
        for name in ('camera', 'screen'):
            session.upload.notice(True, name)
        await session.upload.image_rows(rows, lambda _: None)
        class Socket:
            async def send(self, raw): pass
        session.ws = Socket()
        session.upload.frame_task = asyncio.current_task()
        await session.write({'data': data}, image=True)
        session.upload.frame_task = None
        assert session.upload.slow_sources == {'screen', 'camera'}, 'stream completion cannot steal a requested claim'
        session.upload.written({'data': data})
        assert session.upload.slow and session.upload.notices == [SLOW]
        session.upload.written({'data': data})
        assert not session.upload.slow and session.upload.notices == [SLOW, RECOVERED]
    asyncio.run(scenario())


def test_fixed_fence_prices_all_progress_seen_at_its_completion(monkeypatch):
    """A 20 ms poll must not price current time against a stale, smaller byte count."""
    async def scenario():
        session = Session(None, None, connect=None, observe=lambda *a: None)
        upload = session.upload
        upload.socket_meter = SimpleNamespace(written=100, acknowledged=lambda: 200, rtt=lambda: 0)
        upload.ack_start, upload.started = 0, 1
        monkeypatch.setattr('ouroboros.live_upload.time', SimpleNamespace(monotonic=lambda: 2))
        await upload.measure()
        assert upload.rate == 200, 'later microphone bytes already observed belong to the same elapsed interval'
        await upload.close()
    asyncio.run(scenario())


def test_abstract_buffer_accessor_uses_opaque_fence():
    from ouroboros.live_upload import socket_backlog
    import asyncio
    transport = asyncio.Transport()
    assert socket_backlog(SimpleNamespace(transport=transport)) is None
    session = Session(None, None, connect=None, observe=lambda *a: None)
    session.upload.socket_meter = SimpleNamespace(native=transport, acknowledged=lambda: None)
    assert session.upload.progress() is None
    meter = WireMeter(transport)
    meter.tcp = lambda: 0
    assert meter.acknowledged() is None


@pytest.mark.parametrize('queued', [0, 1000])
def test_empty_microphone_queue_cannot_teach_capacity_below_its_demand(monkeypatch, queued):
    session = Session(None, None, connect=None, observe=lambda *a: None)
    session.ws = object()
    monkeypatch.setattr('ouroboros.live_upload.socket_backlog', lambda ws: queued)
    upload = session.upload
    upload.rate, upload.audio_rate = 50_000, 47_000
    upload.measured_rate(45_000)
    assert upload.rate == (50_000 if queued == 0 else 45_000)
    upload.audio_rate = 0
    upload.measured_rate(40_000)
    assert upload.rate == 40_000, 'muted measurements remain real observations'


def test_retrying_reduced_look_cannot_claim_full_original_recovery(monkeypatch):
    from tests.test_live_upload import screen
    async def scenario():
        monkeypatch.setattr('ouroboros.live_upload.socket_backlog', lambda ws: 0)
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = object()
        upload = session.upload
        upload.rate = 100_000
        original = [{'role':'user', '_call_frame_source':'screen', 'content':[
            {'type':'image_url', 'image_url':{'url':'data:image/jpeg;base64,'+base64.b64encode(screen()).decode()}}]}]
        reduced = await upload.image_rows(original, lambda _: None)
        upload.written(reduced[0]['content'][0]['image_url'])
        assert upload.notices == [SLOW]
        upload.rate = 10_000_000
        retry = await upload.image_rows(reduced, lambda _: None)
        upload.written(retry[0]['content'][0]['image_url'])
        assert upload.notices == [SLOW], 'a retry of the delivery copy is still reduced'
        upload.credit = upload.capacity()  # replenished line credit, independent of this completion test
        full = await upload.image_rows(original, lambda _: None)
        upload.written(full[0]['content'][0]['image_url'])
        assert upload.notices == [SLOW, RECOVERED]
    asyncio.run(scenario())
