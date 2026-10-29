"""Two-sided transport gates, independent of provider vocabulary or image content."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from ouroboros.contracts.live_conversation import Audio, Video
from ouroboros.contracts.realtime import RealtimeSetupError
from ouroboros.realtime_session import JsonSession
from ouroboros.live_upload import Upload, SLOW, RECOVERED


class Session(JsonSession):
    def video_payload(self, frame):
        return {'image': frame.data.hex()}


@pytest.mark.parametrize('cancel', [False, True])
def test_draining_image_never_owns_microphone_lock(cancel):
    async def scenario():
        entered, release, heard = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Socket:
            async def send(self, raw):
                if 'image' in json.loads(raw):
                    entered.set()
                    await release.wait()
                else:
                    heard.set()
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = Socket()
        image = asyncio.create_task(session.write({'image':'synthetic'}, image=True))
        await entered.wait()
        if cancel:
            image.cancel()
        audio = asyncio.create_task(session.write_audio({'audio':'pcm'}, Audio(b'\0\0' * 320, 16000)))
        try:
            await asyncio.wait_for(heard.wait(), 1)
            assert cancel or not image.done(), 'audio must pass while the image writer still awaits drainage'
            await audio
        finally:
            release.set()
            await asyncio.gather(image, audio, return_exceptions=True)
            await session.close()
    asyncio.run(scenario())


def test_pending_frames_are_latest_per_source_and_stop_cancels_them(monkeypatch):
    async def scenario():
        backlog, sent, delivered = [1], [], []
        monkeypatch.setattr('ouroboros.live_upload.socket_backlog', lambda ws: backlog[0])
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = object()
        session.upload.rate, session.upload.audio_rate = 100_000, 47_000
        session.upload.credit = 50_000
        async def send(frame):
            sent.append(frame)
        session.send = send
        live = {'a', 'b', 'stopped'}
        for source, data in [('a',b'old'), ('a',b'latest'), ('b',b'independent'), ('stopped',b'gone')]:
            frame = Video(data, 'image/jpeg', source)
            session.offer_video(frame, lambda frame=frame: delivered.append(frame), lambda source=source: source in live)
        live.remove('stopped')
        await asyncio.sleep(.01)
        assert not sent, 'no frame enters a socket with unsent backlog'
        assert len(session.upload.pending) == 3
        backlog[0] = 0
        await asyncio.wait_for(session.upload.worker, 1)
        assert [(f.source, f.data) for f in sent] == [('a',b'latest'), ('b',b'independent')]
        assert delivered == sent
        session.ws = None
        await session.close()
    asyncio.run(scenario())


def test_audio_capacity_and_episode_notices_have_no_content_gate():
    session = SimpleNamespace()
    upload = Upload(session)
    upload.rate, upload.audio_rate = 50_000, 47_150
    upload.credit = 25_000
    assert upload.budget() == 25_000
    upload.audio_rate = 66_750
    assert upload.budget() == 0, '50 KB/s cannot carry the 24 kHz JSON microphone, even without video'
    for slow in (True, True, True, False, False):
        upload.notice(slow)
    assert upload.notices == [SLOW, RECOVERED]


def test_background_frame_failure_keeps_its_typed_reason(monkeypatch):
    async def scenario():
        monkeypatch.setattr('ouroboros.live_upload.socket_backlog', lambda ws: 0)
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = object()
        session.upload.rate, session.upload.audio_rate, session.upload.credit = 100_000, 47_000, 50_000
        async def send(frame):
            raise RealtimeSetupError('provider_error', 'synthetic refusal')
        session.send = send
        session.offer_video(Video(b'synthetic','image/jpeg','a'), lambda: pytest.fail('not delivered'))
        await session.upload.worker
        with pytest.raises(RealtimeSetupError, match='synthetic refusal'):
            await session.write({'audio':'next'})
        session.ws = None
        await session.close()
    asyncio.run(scenario())


def test_opaque_socket_requires_a_completed_fence_before_a_frame():
    async def scenario():
        pong = asyncio.get_running_loop().create_future()
        class Socket:
            async def ping(self):
                return pong
        session = SimpleNamespace(ws=Socket())
        upload = Upload(session)
        upload.note(4000)
        await asyncio.sleep(0)
        assert upload.backlog() is None
        pong.set_result(None)
        await upload.meter
        assert upload.backlog() == 0 and upload.rate > 0
        await upload.close()
    asyncio.run(scenario())


def test_old_adapter_without_upload_envelope_keeps_its_send_contract():
    from skills.live_conversation.attention import Attention
    async def scenario():
        class OldSession(JsonSession):
            async def send(self, frame):
                sent.append(frame)
        sent = []
        session = OldSession(None, None, connect=None, observe=lambda *a: None)
        attention = Attention(SimpleNamespace(model=SimpleNamespace(vision=True)))
        frame = Video(b'old-style', 'image/jpeg', 'a')
        attention.frames['a'] = frame
        await attention.deliver(session)
        await attention.deliver(session)
        assert sent == [frame] and session.upload.worker is None
        await session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('latest', [b'first', b'newest'])
def test_frame_in_flight_deduplicates_overlapping_looks_but_keeps_new_source_data(monkeypatch, latest):
    async def scenario():
        monkeypatch.setattr('ouroboros.live_upload.socket_backlog', lambda ws: 0)
        entered, release = asyncio.Event(), asyncio.Event()
        sent = []
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = object()
        session.upload.rate, session.upload.audio_rate = 100_000, 47_000
        async def send(frame):
            sent.append((frame.source, frame.data))
            if len(sent) == 1:
                entered.set()
                await release.wait()
        session.send = send
        session.offer_video(Video(b'first','image/jpeg','a'), lambda: None)
        await entered.wait()
        session.offer_video(Video(b'obsolete','image/jpeg','a'), lambda: None)
        session.offer_video(Video(latest,'image/jpeg','a'), lambda: None)
        session.offer_video(Video(b'independent','image/jpeg','b'), lambda: None)
        release.set()
        await asyncio.wait_for(session.upload.worker, 1)
        assert sent == [('a',b'first')] + ([('a',b'newest')] if latest != b'first' else []) + [('b',b'independent')]
        session.ws = None
        await session.close()
    asyncio.run(scenario())


def test_unacknowledged_audio_is_not_an_unsent_frame_backlog(monkeypatch):
    import struct
    from ouroboros.live_upload import WireMeter
    writable = [True]
    class Socket:
        def setsockopt(self, level, option, value):
            assert (option, value) == (0x201, 1)
        def getsockopt(self, *args):
            info = bytearray(104)
            struct.pack_into('=I', info, 32, 4800)  # 100ms of continuous audio still in flight
            return bytes(info)
    sock = Socket()
    monkeypatch.setattr('ouroboros.live_upload.sys.platform', 'darwin')
    monkeypatch.setattr('ouroboros.live_upload.select.select', lambda *a: ([], [sock] if writable[0] else [], []))
    native = SimpleNamespace(get_extra_info=lambda key: sock, get_write_buffer_size=lambda: 0)
    meter = WireMeter(native)
    meter.written = 10000
    assert meter.acknowledged() == 5200
    assert meter.unsent() == 0, 'in-flight audio cannot starve video on a higher-RTT connection'
    writable[0] = False
    assert meter.unsent() > 0, 'actually unsent data must still prevent an image write'


def test_tls_counter_sits_below_encryption_and_keeps_public_transport(monkeypatch):
    from ouroboros.live_upload import bind_meter
    written = []
    native = SimpleNamespace(get_extra_info=lambda key: None, write=written.append)
    protocol = SimpleNamespace(_transport=native)
    tls = SimpleNamespace(_ssl_protocol=protocol)
    ws = SimpleNamespace(transport=tls)
    meter = bind_meter(ws)
    protocol._transport.write(b'encrypted record including framing')
    assert ws.transport is tls and meter.written == len(written[0])


def test_audio_reservation_measures_encoded_bytes_including_compression():
    from ouroboros.live_upload import AUDIO_WRITE, WireMeter
    upload = SimpleNamespace(audio_rate=0)
    native = SimpleNamespace(get_extra_info=lambda key: None, write=lambda data: None)
    meter = WireMeter(native, upload)
    token = AUDIO_WRITE.set({'upload':upload, 'seconds':.02, 'bytes':0})
    try:
        meter.write(b'x' * 300)
        meter.write(b'x' * 22)
    finally:
        AUDIO_WRITE.reset(token)
    assert upload.audio_rate == 322 / .02
    meter.write(b'not microphone')
    assert upload.audio_rate == 322 / .02


def test_microphone_and_small_controls_cannot_replace_a_drain_measurement():
    async def scenario():
        wire = SimpleNamespace(written=0, acknowledged=lambda: wire.written, rtt=lambda: 0)
        upload = Upload(SimpleNamespace())
        upload.socket_meter = wire
        upload.rate, upload.audio_rate = 80_000, 66_750
        upload.note(1335, calibrate=False)
        upload.note(200, calibrate=True)
        assert upload.rate == 80_000 and upload.meter is None
        upload.note(40_000, calibrate=True)
        wire.written += 40_000
        await upload.meter
        assert upload.rate > 0 and upload.meter.done(), 'a real image batch must refresh the drain measurement'
        await upload.close()
    asyncio.run(scenario())


def test_muted_microphone_does_not_prevent_a_frame_using_measured_capacity():
    upload = Upload(SimpleNamespace())
    upload.rate = 100_000
    assert upload.budget() == 50_000
    upload.audio_rate = 100_001
    assert upload.budget() == 0


def test_page_audio_backlog_counts_pcm_even_when_a_video_is_queued():
    from tests._live_stack_page import Page, jpeg
    from tests.test_live_upload import pending_page_audio
    page = Page()
    page.tick()
    page.video(jpeg())
    page.tick()
    assert pending_page_audio(page) == 2 * 960 * 2


def test_a_deferred_look_cannot_replace_a_newer_waiting_live_frame(monkeypatch):
    import base64
    from hashlib import sha256
    from skills.live_conversation.attention import Attention
    from tests._live_stack_page import jpeg
    async def scenario():
        monkeypatch.setattr('ouroboros.live_upload.socket_backlog', lambda ws: 1)
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = object()
        session.upload.rate, session.upload.audio_rate = 100_000, 47_000
        attention = Attention(SimpleNamespace())
        old, latest = Video(jpeg('old'), 'image/jpeg', 'screen'), Video(jpeg('new'), 'image/jpeg', 'screen')
        attention.frames['screen'] = latest
        row = {'_call_frame_source':'screen', '_call_frame_digest':sha256(old.data).hexdigest(),
               'content':[{'type':'image_url', 'image_url':{'url':'data:image/jpeg;base64,' + base64.b64encode(old.data).decode()}}]}
        result = await attention.delivery_images(session, [row])
        assert session.upload.pending['screen'][0] is latest
        assert result[0]['_deferred_frame'] and 'not been delivered' in result[0]['content'][0]['text']
        assert row['content'][0]['type'] == 'image_url', 'the canonical original is unchanged'
        assert not attention.seen
        session.ws = None
        await session.close()
    asyncio.run(scenario())
