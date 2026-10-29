"""R23 portable end-to-end receipts and stopped-source regressions, synthetic only."""
import asyncio
import base64
import json
import time

import pytest

from ouroboros.contracts.live_conversation import Video, Audio
from ouroboros.live_upload import SLOW, RECOVERED
from tests.test_live_upload_controls import Session
from tests.test_live_upload import screen, video_bytes, measurement_arm
from tests.test_live_conversation import host_world as host_world
from tests._live_stack import stack
from tests._live_upload_wire import limit_connector


@pytest.mark.parametrize('requested', [False, True])
@pytest.mark.parametrize('stop', [False, True])
def test_completion_respects_source_lifetime(monkeypatch, requested, stop):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Socket:
            async def send(self, raw):
                entered.set()
                await release.wait()
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = Socket()
        upload = session.upload
        upload.rate = 100_000
        live = {'screen'}
        data = screen()
        if requested:
            rows = await upload.image_rows([{'_call_frame_source':'screen','content':[
                {'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+base64.b64encode(data).decode()}}]}], lambda _: None)
            task = asyncio.create_task(session.write({'image': rows[0]['content'][0]['image_url']['url']}, image=True))
        else:
            async def send(frame):
                entered.set()
                await release.wait()
            session.send = send
            upload.offer(Video(data,'image/jpeg','screen'), lambda: None, lambda: 'screen' in live)
            task = upload.worker
        await entered.wait()
        assert upload.slow_sources == {'screen'}
        if stop:
            live.clear()
            upload.forget('screen')
            assert not upload.slow
        release.set()
        await task
        observed = dict(requested=requested, stop=stop, slow=upload.slow, sources=list(upload.slow_sources), notices=upload.notices)
        await upload.close()
        assert observed['slow'] is (not stop), observed
        assert observed['notices'] == ([SLOW,RECOVERED] if stop else [SLOW]), observed
    asyncio.run(scenario())



@pytest.mark.parametrize('hidden', [False, True])
@pytest.mark.parametrize('engine,thinking,rate', [('gemini', False, 50_000), ('gemini', True, 50_000), ('openai', False, 80_000)])
def test_hidden_downstream_queue_never_promotes_buffer_acceptance_to_throughput(host_world, monkeypatch, engine, thinking, rate, hidden):
    from tests._live_stack_wire import NativeWire
    async def scenario():
        measurement_arm(monkeypatch, 'hidden' if hidden else 'visible')
        limit_connector(monkeypatch, rate)
        audio, sizes = [], []
        native = NativeWire.send
        async def observed(wire, raw):
            event = json.loads(raw)
            if event.get('type') == 'input_audio_buffer.append' or 'audio' in event.get('realtimeInput', {}):
                audio.append(time.monotonic())
            frame = video_bytes(event)
            if frame is not None:
                sizes.append(len(frame))
            await native(wire, raw)
        monkeypatch.setattr(NativeWire, 'send', observed)
        data = screen()
        async with stack(host_world, monkeypatch, engine, thinking=thinking) as run:
            run.call.attention.mode = 'stream'
            await run.wait(lambda: len(audio) >= 10)
            first = len(audio)
            for number in range(6):
                run.page.video(data[:2] + b'\xff\xfe\x00\x03' + bytes([number]) + data[2:])
                await asyncio.sleep(1)
            await run.wait(lambda: len(audio) > first + 10)
            gaps = [b-a for a,b in zip(audio[first:], audio[first+1:])]
            evidence = dict(engine=engine, hidden=hidden, sizes=sizes, max_gap=max(gaps), rate=run.call.session.upload.rate)
            print('HIDDEN_QUEUE', evidence)
            assert len(sizes) > 1 and max(sizes) * 4/3 <= rate * .5, evidence
            assert max(gaps) < 1, evidence
    asyncio.run(scenario())


@pytest.mark.parametrize('second_write', [False, True])
def test_opening_writes_during_a_pong_cannot_strand_frames(second_write):
    async def scenario():
        pongs, sent = [], []
        class Socket:
            async def ping(self):
                future = asyncio.get_running_loop().create_future()
                pongs.append(future)
                return future
            async def send(self, raw):
                sent.append(json.loads(raw))
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = Socket()
        setup = asyncio.create_task(session.upload.setup())
        while not pongs:
            await asyncio.sleep(0)
        await session.write({'setup': 'first'})
        if second_write:
            await session.write({'history': 'second'})
        pongs[0].set_result(None)
        await setup
        first = asyncio.create_task(session.write({'image': 'first'}, image=True))
        while len(pongs) < 2:
            await asyncio.sleep(0)
        pongs[1].set_result(None)  # preflight clears earlier audio/control bytes
        await first
        while len(pongs) < 3:
            await asyncio.sleep(0)
        pending = asyncio.create_task(session.write({'image': 'second'}, image=True))
        await session.write_audio({'audio':'live'}, Audio(b'\0\0' * 320, 16000))
        await asyncio.sleep(0)
        assert not pending.done() and sent[-1] == {'audio': 'live'}
        pongs[2].set_result(None)
        while len(pongs) < 4:
            await asyncio.sleep(0)
        pongs[3].set_result(None)
        await pending
        assert sent[-1] == {'image':'second'}
        await session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('restart', [False, True])
def test_retired_generation_cannot_change_restarted_source(restart):
    async def scenario():
        session = Session(None, None, connect=None, observe=lambda *a: None)
        upload = session.upload
        upload.rate = 100_000
        frame = Video(b'original', 'image/jpeg', 'screen')
        copy = Video(b'copy', 'image/jpeg', 'screen')
        upload.notice(True, 'screen')
        generation = upload.generations.get('screen', 0)
        upload.forget('screen')
        if restart:
            upload.pending['screen'] = (frame, lambda: None, lambda: True)
        upload.completed(frame, copy, generation=generation)
        assert not upload.slow and upload.notices == [SLOW, RECOVERED]
        await upload.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('elapsed,rtt,expected', [(0, 0, None), (.1, .2, 10_000), (.1, .05, 20_000)])
def test_frame_sample_uses_positive_elapsed_without_manufacturing_rate(monkeypatch, elapsed, rtt, expected):
    from types import SimpleNamespace
    async def scenario():
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.upload.rtt = rtt
        monkeypatch.setattr('ouroboros.live_upload.time', SimpleNamespace(monotonic=lambda: elapsed))
        pong = asyncio.get_running_loop().create_future()
        pong.set_result(None)
        await session.upload.measure(1000, 0, pong)
        assert session.upload.error is None
        assert session.upload.rate == expected
        await session.close()
    asyncio.run(scenario())


def test_requested_source_restart_does_not_inherit_retired_write():
    from tests._live_stack_page import jpeg
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Socket:
            async def send(self, raw):
                entered.set()
                await release.wait()
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = Socket()
        upload = session.upload
        upload.rate = 10_000_000
        encoded = base64.b64encode(jpeg()).decode()
        rows = [{'_call_frame_source':'screen', 'content':[{'type':'image_url',
                'image_url':{'url':'data:image/jpeg;base64,' + encoded}}]}]
        await upload.image_rows(rows, lambda _: None)
        old = asyncio.create_task(session.write({'image': encoded}, image=True))
        await entered.wait()
        upload.forget('screen')
        assert not upload.deliveries
        upload.notice(True, 'screen')
        await upload.image_rows(rows, lambda _: None)
        release.set()
        await old
        assert upload.slow and upload.deliveries, 'old completed pixels cannot settle the restarted source claim'
        await session.write({'image': encoded}, image=True)
        assert not upload.slow and not upload.deliveries
        await upload.image_rows(rows, lambda _: None)
        await session.close()
        assert not upload.deliveries and upload.slot is None
    asyncio.run(scenario())
