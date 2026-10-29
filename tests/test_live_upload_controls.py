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
        sent, delivered = [], []
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = object()
        session.upload.rate, session.upload.audio_rate = 100_000, 47_000
        session.upload.slot = Video(b'earlier', 'image/jpeg', 'other')
        async def send(frame):
            sent.append(frame)
        session.send = send
        live = {'a', 'b', 'stopped'}
        for source, data in [('a',b'old'), ('a',b'latest'), ('b',b'independent'), ('stopped',b'gone')]:
            frame = Video(data, 'image/jpeg', source)
            session.offer_video(frame, lambda frame=frame: delivered.append(frame), lambda source=source: source in live)
        live.remove('stopped')
        await asyncio.sleep(.01)
        assert not sent, 'no second frame before the first finishes'
        assert len(session.upload.pending) == 3
        session.upload.slot = None
        await asyncio.wait_for(session.upload.worker, 1)
        assert [(f.source, f.data) for f in sent] == [('a',b'latest'), ('b',b'independent')]
        assert delivered == sent
        session.ws = None
        await session.close()
    asyncio.run(scenario())


def test_upload_episode_notices_have_no_content_gate():
    session = SimpleNamespace()
    upload = Upload(session)
    for slow in (True, True, True, False, False):
        upload.notice(slow)
    assert upload.notices == [SLOW, RECOVERED]


def test_background_frame_failure_keeps_its_typed_reason(monkeypatch):
    async def scenario():
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = object()
        session.upload.rate, session.upload.audio_rate = 100_000, 47_000
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
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = object()
        session.upload.slot = Video(b'earlier', 'image/jpeg', 'other')
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
