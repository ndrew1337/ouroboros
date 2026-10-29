"""R22 opposite controls for physical delivery, independent of OS/provider labels."""
import asyncio
import base64
from types import SimpleNamespace

import pytest

from ouroboros.contracts.live_conversation import Audio, Video
from ouroboros.live_upload import SLOW, RECOVERED
from tests.test_live_upload_controls import Session






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
        session = Session(None, None, connect=None, observe=lambda *a: None)
        written, done = [], []
        class Socket:
            count = 0
            async def send(self, raw):
                written.append(raw)
            async def ping(self):
                self.count += 1
                pong = asyncio.get_running_loop().create_future()
                if lost and self.count == 2:
                    pong.set_exception(OSError('lost frame receipt'))
                else:
                    pong.set_result(None)
                return pong
        session.ws = Socket()
        session.upload.rate = 100_000
        async def send(frame):
            await session.write(session.video_payload(frame), image=True)
        session.send = send
        frame = Video(b'original', 'image/jpeg', 'a')
        session.offer_video(frame, lambda: done.append(frame))
        await asyncio.wait_for(session.upload.worker, 2)
        await session.upload.meter
        assert done == [frame] and len(written) == 1, 'a lost receipt cannot silently resend completed bytes'
        assert isinstance(session.upload.error, OSError) is lost
        await session.upload.close()
    asyncio.run(scenario())


def test_requested_delivery_recovers_only_after_successful_write(monkeypatch):
    from tests.test_live_upload import screen
    async def scenario():
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




def test_equal_pixels_keep_independent_source_completion(monkeypatch):
    from tests._live_stack_page import jpeg
    async def scenario():
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = object()
        session.upload.rate = 10_000_000
        data = base64.b64encode(jpeg()).decode()
        rows = [{'_call_frame_source': name, 'content': [{'type': 'image_url',
                'image_url': {'url': 'data:image/jpeg;base64,' + data}}]} for name in ('camera', 'screen')]
        for name in ('camera', 'screen'):
            session.upload.notice(True, name)
        first = await session.upload.image_rows(rows, lambda _: None)
        assert first[1]['_deferred_frame'], 'only one image may be admitted at once'
        session.upload.written({'data': data})
        assert session.upload.slow_sources == {'screen'}
        await session.upload.image_rows(rows[1:], lambda _: None)
        session.upload.written({'data': data})
        assert session.upload.notices == [SLOW, RECOVERED]
        await session.upload.close()
    asyncio.run(scenario())


def test_retrying_reduced_look_cannot_claim_full_original_recovery(monkeypatch):
    from tests.test_live_upload import screen
    async def scenario():
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
        full = await upload.image_rows(original, lambda _: None)
        upload.written(full[0]['content'][0]['image_url'])
        assert upload.notices == [SLOW, RECOVERED]
    asyncio.run(scenario())
