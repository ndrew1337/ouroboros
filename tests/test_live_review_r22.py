"""R22 regressions adapted from the reviewer probes; synthetic loopback only."""
import asyncio
import json

import pytest

from tests._live_stack import stack
from tests._live_stack_page import jpeg
from tests._live_upload_wire import limit_connector
from tests.test_live_conversation import host_world as host_world
from tests.test_live_upload import video_bytes, warm_upload, measurement_arm


@pytest.mark.parametrize('engine,thinking', [('gemini', False), ('gemini', True), ('openai', False)])
@pytest.mark.parametrize('opaque', ['visible', 'hidden', 'opaque'])
def test_fast_transport_keeps_delivering_after_continuous_audio(host_world, monkeypatch, engine, thinking, opaque):
    async def scenario():
        limit_connector(monkeypatch, 10_000_000)
        measurement_arm(monkeypatch, opaque)
        async with stack(host_world, monkeypatch, engine, thinking=thinking) as run:
            run.call.attention.mode = 'stream'
            upload = run.call.session.upload
            await warm_upload(run)
            await run.wait(lambda: len(run.wire.audio) >= 9600)
            original = jpeg('synthetic fast frame')
            before = len(run.wire.audio)
            run.page.video(original)
            # Observe ongoing transport progress, not a particular scheduling latency.
            await run.wait(lambda: len(run.wire.audio) >= before + 48000)
            facts = dict(
                opaque=opaque, pending=list(upload.pending), rate=upload.rate,
                audio_rate=upload.audio_rate, busy=upload.busy())
            print('OPAQUE_TRANSPORT', facts)
            assert any(video_bytes(v) == original for v in run.wire.sent), facts
    asyncio.run(scenario())


def test_a_small_source_does_not_clear_another_sources_slow_episode(monkeypatch):
    import base64
    from ouroboros.contracts.live_conversation import Video
    from ouroboros.live_upload import SLOW, RECOVERED
    from ouroboros.realtime_session import JsonSession
    from tests.test_live_upload import screen
    async def scenario():
        class Session(JsonSession):
            def video_payload(self, frame):
                return {'image': base64.b64encode(frame.data).decode()}
            async def send(self, frame):
                delivered.append(frame)
        delivered = []
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = object()
        session.upload.rate, session.upload.audio_rate = 100_000, 47_000
        large = Video(screen(), 'image/jpeg', 'shared-screen')
        import io
        from PIL import Image
        encoded = io.BytesIO()
        Image.new('RGB', (16, 16), 'red').save(encoded, format='JPEG')
        small = Video(encoded.getvalue(), 'image/jpeg', 'meeting-camera')
        try:
            for _ in range(2):
                session.offer_video(large, lambda: None)
                session.offer_video(small, lambda: None)
                await asyncio.wait_for(session.upload.worker, 2)
            assert len(delivered) == 4
            assert all(len(frame.data) < len(large.data) for frame in delivered if frame.source == large.source)
            assert session.upload.notices == [SLOW], session.upload.notices
            session.upload.rate = 10_000_000
            session.offer_video(large, lambda: None)
            await asyncio.wait_for(session.upload.worker, 2)
            assert delivered[-1] is large
            assert session.upload.notices == [SLOW, RECOVERED]
        finally:
            session.ws = None
            await session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('engine,thinking', [('gemini', False), ('gemini', True), ('openai', False)])
def test_requested_full_look_reports_recovery(host_world, monkeypatch, engine, thinking):
    from tests.test_live_upload import screen
    from ouroboros.live_upload import SLOW, RECOVERED
    async def scenario():
        limit_connector(monkeypatch, 10_000_000)
        async with stack(host_world, monkeypatch, engine, thinking=thinking) as run:
            run.call.attention.mode = 'asked'
            upload = run.call.session.upload
            await warm_upload(run)
            bootstrap_worker = upload.worker
            run.page.send({'type': 'mute', 'muted': True})
            await run.wait(lambda: all(peer.muted for peer in run.host.hub._peers.values()))
            await run.wait(lambda: not upload.audio_waiters and not upload.busy())
            first = screen()
            run.page.video(first)
            await run.wait(lambda: any(f.data == first for f in run.call.attention.frames.values()))
            upload.rate = 100_000  # inject a measured constrained capacity; keep the actual socket fast
            start = len(run.wire.sent)
            await run.tool('limited-look', 'attend', {'look': True})
            await run.wait(lambda: 'limited-look' in run.wire.outputs)
            await run.wait(lambda: any(SLOW in str(row) for row in run.host.messages))
            await run.wait(lambda: any(0 < len(video_bytes(v) or b'') < len(first) for v in run.wire.sent[start:]))
            await run.quiet()
            second = first[:2] + b'\xff\xfe\x00\x03x' + first[2:]
            run.page.video(second)
            await run.wait(lambda: any(f.data == second for f in run.call.attention.frames.values()))
            upload.rate = 10_000_000
            start = len(run.wire.sent)
            await run.tool('full-look', 'attend', {'look': True})
            await run.wait(lambda: 'full-look' in run.wire.outputs)
            await run.wait(lambda: any(video_bytes(v) == second for v in run.wire.sent[start:]))
            await run.quiet()
            assert upload.worker is bootstrap_worker, 'this probe qualifies direct requested looks, not a deferred worker'
            assert sum(RECOVERED in str(row) for row in run.host.messages) == 1, dict(
                slow=upload.slow, notices=upload.notices, slow_state=run.host.state.get('upload_slow'))
    asyncio.run(scenario())


@pytest.mark.parametrize('release_inactive_reservation', [False, True])
@pytest.mark.parametrize('engine,thinking,weak_rate', [('gemini', False, 30_000), ('gemini', True, 30_000), ('openai', False, 50_000)])
def test_muting_releases_the_microphone_reservation(host_world, monkeypatch, release_inactive_reservation, engine, thinking, weak_rate):
    import io
    from PIL import Image
    async def scenario():
        links = limit_connector(monkeypatch, 100_000)
        async with stack(host_world, monkeypatch, engine, thinking=thinking) as run:
            upload = run.call.session.upload
            run.call.attention.mode = 'stream'
            await run.wait(lambda: upload.audio_rate > weak_rate)
            run.page.send({'type': 'mute', 'muted': True})
            await run.wait(lambda: all(peer.muted for peer in run.host.hub._peers.values()))
            # A physical observation below the previous audio rate, on an empty,
            # now-muted socket. There is ample room for this small image alone.
            for link in links:
                link.rate = weak_rate
            upload.rate = weak_rate
            if release_inactive_reservation:
                upload.audio_rate = 0  # two-sided causal control: the observed microphone is explicitly muted
            encoded = io.BytesIO()
            Image.new('RGB', (16, 16), 'blue').save(encoded, format='JPEG')
            original = encoded.getvalue()
            run.page.video(original)
            await run.wait(lambda: bool(run.call.attention.frames))
            await asyncio.sleep(.5)
            assert any(video_bytes(v) == original for v in run.wire.sent), dict(
                audio_rate=upload.audio_rate, rate=upload.rate, capacity=upload.capacity(), busy=upload.busy())
    asyncio.run(scenario())


def test_resize_cpu_evidence():
    import time
    from ouroboros.contracts.live_conversation import Video
    from ouroboros.live_upload import smaller_frame
    from tests.test_live_upload import screen
    frame = Video(screen(), 'image/jpeg', 'synthetic')
    evidence = []
    for limit in (len(frame.data), 30_000, 3000, 100):
        started = time.process_time()
        for _ in range(20):
            copy = smaller_frame(frame, limit)
        evidence.append(dict(limit=limit, result_bytes=len(copy.data) if copy else None,
            cpu_ms=(time.process_time() - started) * 1000 / 20))
    print('RESIZE_CPU', evidence)


@pytest.mark.parametrize('engine,thinking', [('gemini', False), ('gemini', True), ('openai', False), ('grok', False)])
@pytest.mark.parametrize('break_offer', [False, True])
def test_handoff_mail_offer_keeps_custody_until_its_write_succeeds(host_world, monkeypatch, engine, thinking, break_offer):
    from ouroboros.contracts.live_conversation import ContextUpdate
    from tests.test_live_stack_preparation import request
    async def scenario():
        async with stack(host_world, monkeypatch, engine, thinking=thinking) as run:
            old = run.wire
            original = type(run.call.session).send
            broken = []
            async def send(session, command):
                if (break_offer and not broken and run.host.connection_number == 2
                        and isinstance(command, ContextUpdate) and command.respond
                        and any('New room context is in the fitted history' in str(row) for row in command.messages)):
                    broken.append(True)
                    await session.ws.close(code=1012, reason='Synthetic loss while sending the pending-mail offer')
                return await original(session, command)
            monkeypatch.setattr(type(run.call.session), 'send', send)
            old.input_start('owner-holds-mail-switch')
            await run.wait(lambda: run.call.input_open)
            await request(run)
            await run.wait(lambda: run.call.preparing.done())
            text = 'Synthetic room request whose post-handoff offer meets a closed socket.'
            run.mail.put({'text': text, 'msg_id': 'review-mail-offer-loss'})
            await run.wait(lambda: any('New room context is in the fitted history' in str(row) for row in run.call.pending_offers))
            run.provider.script.append(('say', 'Old connection answered the spoken owner turn.'))
            old.input_end('owner-holds-mail-switch', 'Synthetic spoken turn before the handoff.')
            await run.wait(lambda: run.host.connection_number == (3 if break_offer else 2))
            await run.quiet()
            # Allow the normal offer worker to process an event-loop turn after quiet.
            await asyncio.sleep(.1)
            assert any(text in str(row) for row in run.host.messages)
            assert len(run.wire.spoken) == 1, dict(
                broken=broken, spoken=run.wire.spoken, pending=run.call.pending_offers,
                mail_marker=getattr(run.host, '_pending_mail_answer', False),
                connections=len(run.provider.connections))
    asyncio.run(scenario())


@pytest.mark.parametrize('thinking', [False, True])
@pytest.mark.parametrize('deferred', [False, True])
def test_gemini_attend_delivers_pixels_or_explicit_deferred_words(host_world, monkeypatch, thinking, deferred):
    async def scenario():
        limit_connector(monkeypatch, 10_000_000)
        async with stack(host_world, monkeypatch, 'gemini', thinking=thinking) as run:
            run.call.attention.mode = 'asked'
            upload = run.call.session.upload
            await warm_upload(run)
            if deferred:
                monkeypatch.setattr(upload, 'capacity', lambda: 0)
            requested = jpeg('synthetic requested look')
            run.page.video(requested)
            await run.wait(lambda: any(f.data == requested for f in run.call.attention.frames.values()))
            start = len(run.wire.sent)
            await run.tool('review-look', 'attend', {'look': True})
            await run.wait(lambda: 'review-look' in run.wire.outputs)
            sent = run.wire.sent[start:]
            delivered = any(video_bytes(v) is not None for v in sent)
            words = json.dumps(sent)
            assert delivered or 'These pixels have not been delivered' in words, sent
            assert delivered is not deferred
            response = next(r for event in sent for r in event.get('toolResponse', {}).get('functionResponses', [])
                            if r['id'] == 'review-look')
            assert ('These pixels have not been delivered' in json.dumps(response)) is deferred
    asyncio.run(scenario())


@pytest.mark.parametrize('engine', ['gemini', 'openai'])
def test_history_look_replay_keeps_reader_without_unpriced_original(host_world, monkeypatch, engine):
    """A fresh connection has no drainage sample: its history cannot spend old credit."""
    import base64
    from tests.test_live_upload import screen
    from tests.test_live_stack_preparation import request
    async def scenario():
        limit_connector(monkeypatch, 80_000)
        async with stack(host_world, monkeypatch, engine) as run:
            original = screen()
            path = await run.host.retain_frame(original, 'image/jpeg', source='screen-history')
            row = {'role':'user', '_call_frame_source':'screen-history', 'content':[
                {'type':'text', 'text':f'Exact requested look: {path}'},
                {'type':'image_url', 'image_url':{'url':'data:image/jpeg;base64,' + base64.b64encode(original).decode()},
                 '_source_path':path}]}
            run.host.messages.append(row)
            await request(run)
            await run.wait(lambda: run.host.connection_number == 2)
            await run.quiet()
            sent = json.dumps(run.wire.sent)
            assert base64.b64encode(original).decode() not in sent, 'renewal may not replay an unpriced full-size look'
            assert 'These pixels have not been delivered' in sent and path in sent
            assert row in run.host.messages and row['content'][1]['type'] == 'image_url'
            from pathlib import Path
            assert Path(path).read_bytes() == original
    asyncio.run(scenario())


@pytest.mark.parametrize('meeting', [False, True])
def test_mute_releases_only_the_last_microphone_and_unmute_reserves_again(host_world, monkeypatch, meeting):
    from ouroboros import call_protocol as cp
    async def scenario():
        async with stack(host_world, monkeypatch, 'gemini') as run:
            upload = run.call.session.upload
            await run.wait(lambda: upload.audio_rate > 0)
            if meeting:
                run.host.hub.join(cp.PeerInfo('meeting', cp.PEER_MEETING, True, False), 48000)
                await run.wait(lambda: 'meeting' in run.call.peer_info)
            run.page.send({'type':'mute', 'muted':True})
            await run.wait(lambda: bool(run.call.muted_peers))
            await asyncio.sleep(.05)
            assert bool(upload.audio_rate) is meeting
            if meeting:
                run.host.hub.leave('meeting', 'finished')
                await run.wait(lambda: 'meeting' not in run.call.peer_info)
                assert upload.audio_rate == 0
            run.page.send({'type':'mute', 'muted':False})
            await run.wait(lambda: upload.audio_rate > 0)
    asyncio.run(scenario())
