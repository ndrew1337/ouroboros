"""Opposite controls for R23 completion lifetime and good-link delivery."""
import asyncio
import base64
from types import SimpleNamespace
import pytest
from ouroboros.contracts.live_conversation import Video
from ouroboros.live_upload import WireMeter, SLOW, RECOVERED
from tests.test_live_upload_controls import Session
from tests.test_live_upload import screen, video_bytes
from tests.test_live_conversation import host_world as host_world
from tests._live_stack import stack
from tests._live_upload_wire import limit_connector


@pytest.mark.parametrize('requested', [False, True])
@pytest.mark.parametrize('stop', [False, True])
def test_completion_respects_source_lifetime(monkeypatch, requested, stop):
    async def scenario():
        monkeypatch.setattr('ouroboros.live_upload.socket_backlog', lambda ws: 0)
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


@pytest.mark.parametrize('engine,thinking', [('gemini',False),('gemini',True),('openai',False)])
@pytest.mark.parametrize('no_kernel', [False,True])
def test_good_link_keeps_three_full_frames_at_page_cadence(host_world, monkeypatch, engine, thinking, no_kernel):
    async def scenario():
        if no_kernel:
            monkeypatch.setattr(WireMeter,'unsent',lambda self:None)
            monkeypatch.setattr(WireMeter,'acknowledged',lambda self:None)
        limit_connector(monkeypatch,10_000_000)
        async with stack(host_world,monkeypatch,engine,thinking=thinking) as run:
            run.call.attention.mode='stream'
            await run.wait(lambda: run.call.session.upload.rate is not None)
            data=screen()
            expected=[]
            for number in range(3):
                frame=data[:2]+b'\xff\xfe\x00\x03'+bytes([number])+data[2:]
                expected.append(frame)
                run.page.video(frame)
                await asyncio.sleep(1)
            received=[video_bytes(v) for v in run.wire.sent if video_bytes(v) is not None]
            assert received==expected, (list(map(len,received)),list(map(len,expected)))
            assert not any(SLOW in str(r) or RECOVERED in str(r) for r in run.host.messages)
    asyncio.run(scenario())


@pytest.mark.parametrize('platform', ['darwin','linux','win32'])
def test_native_probe_unavailable_uses_buffer_without_crashing(monkeypatch,platform):
    import ouroboros.live_upload as m
    class Socket:
        def setsockopt(self,*args): raise OSError('unsupported synthetic option')
        def getsockopt(self,*args): raise OSError('unsupported synthetic option')
        def fileno(self): return -1
    monkeypatch.setattr(m,'sys',SimpleNamespace(platform=platform))
    native=SimpleNamespace(get_extra_info=lambda _:Socket(),get_write_buffer_size=lambda:123)
    meter=m.WireMeter(native)
    assert meter.unsent() is None
    assert meter.acknowledged() is None
    assert m.socket_backlog(SimpleNamespace(_upload_meter=meter))==123


@pytest.mark.parametrize('engine,thinking,rate', [('gemini',False,50_000),('gemini',True,50_000),('openai',False,80_000)])
def test_stand_sized_frame_behind_hidden_queue(host_world, monkeypatch, engine, thinking, rate):
    """The owner's measured ~130 KB pressure, synthetic pixels, no copied call data."""
    import json, time
    from tests._live_stack_page import jpeg
    from tests._live_stack_wire import NativeWire
    from tests._live_upload_wire import LimitedTransport
    async def scenario():
        frame=jpeg('synthetic bounded upload probe')
        while len(frame)<130_000:
            size=min(65_533,130_000-len(frame)-4)
            if size<0: break
            frame=frame[:2]+b'\xff\xfe'+(size+2).to_bytes(2,'big')+b'x'*size+frame[2:]
        assert len(frame)==130_000
        monkeypatch.setattr(LimitedTransport,'get_write_buffer_size',
            lambda self:max(0,self.queued-256*1024)+self.native.get_write_buffer_size())
        monkeypatch.setattr(WireMeter,'unsent',lambda self:None)
        monkeypatch.setattr(WireMeter,'acknowledged',lambda self:None)
        limit_connector(monkeypatch,rate)
        audio=[]
        original_send=NativeWire.send
        async def observe(wire,raw):
            event=json.loads(raw)
            if event.get('type')=='input_audio_buffer.append' or 'audio' in event.get('realtimeInput',{}):
                audio.append(time.monotonic())
            await original_send(wire,raw)
        monkeypatch.setattr(NativeWire,'send',observe)
        async with stack(host_world,monkeypatch,engine,thinking=thinking) as run:
            run.call.attention.mode='stream'
            await run.wait(lambda:len(audio)>=10)
            start=len(audio)
            measured=run.call.session.upload.rate
            run.page.video(frame)
            await run.wait(lambda:any(video_bytes(v) is not None for v in run.wire.sent))
            after=len(audio)
            await run.wait(lambda:len(audio)>after+2)
            sizes=[len(video_bytes(v)) for v in run.wire.sent if video_bytes(v) is not None]
            gaps=[b-a for a,b in zip(audio[start-1:],audio[start:])]
            observed=dict(engine=engine,thinking=thinking,rate=rate,measured=measured,
                          sizes=sizes,max_audio_gap=max(gaps))
            print('STAND_SIZE',observed)
            assert max(gaps)<1 and max(sizes)*4/3<=rate*.5,observed
    asyncio.run(scenario())


@pytest.mark.parametrize('restart', [False, True])
def test_retired_generation_cannot_change_restarted_source(monkeypatch, restart):
    async def scenario():
        monkeypatch.setattr('ouroboros.live_upload.socket_backlog', lambda ws: 0)
        entered, release = asyncio.Event(), asyncio.Event()
        session = Session(None, None, connect=None, observe=lambda *a: None)
        session.ws = object()
        upload = session.upload
        upload.rate = 100_000
        live = {'screen'}
        async def send(frame):
            entered.set()
            await release.wait()
        session.send = send
        upload.offer(Video(screen(), 'image/jpeg', 'screen'), lambda: None, lambda: 'screen' in live)
        await entered.wait()
        live.clear()
        upload.forget('screen')
        if restart:
            live.add('screen')  # same source id and bytes, but a new share
        release.set()
        await upload.worker
        assert not upload.slow and upload.notices == [SLOW, RECOVERED]
        await upload.close()
    asyncio.run(scenario())


def test_requested_source_restart_does_not_inherit_retired_write(monkeypatch):
    from tests._live_stack_page import jpeg
    async def scenario():
        monkeypatch.setattr('ouroboros.live_upload.socket_backlog', lambda ws: 0)
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
        rows = [{'_call_frame_source': 'screen', 'content': [{'type': 'image_url',
                'image_url': {'url': 'data:image/jpeg;base64,' + encoded}}]}]
        await upload.image_rows(rows, lambda _: None)
        old = asyncio.create_task(session.write({'image': encoded}, image=True))
        await entered.wait()
        upload.forget('screen')
        assert not upload.deliveries
        upload.notice(True, 'screen')
        await upload.image_rows(rows, lambda _: None)
        release.set()
        await old
        assert upload.slow and upload.deliveries, 'retired write cannot settle the restarted source'
        await session.write({'image': encoded}, image=True)
        assert not upload.slow and not upload.deliveries
        await upload.image_rows(rows, lambda _: None)
        await session.close()
        assert not upload.deliveries
    asyncio.run(scenario())


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('elapsed', [0, .1])
def test_rate_samples_have_positive_elapsed(monkeypatch, native, elapsed):
    from ouroboros.live_upload import Upload
    async def scenario():
        pong = asyncio.get_running_loop().create_future()
        pong.set_result(None)
        class Socket:
            async def ping(self): return pong
        upload = Upload(SimpleNamespace(ws=Socket()))
        upload.started, upload.bytes, upload.total_bytes = 1., 1000, 1000
        upload.fence_bytes = 1000
        if native:
            upload.socket_meter = SimpleNamespace(written=1000, acknowledged=lambda: 1000, rtt=lambda: 0)
            upload.ack_start = 0
        monkeypatch.setattr('ouroboros.live_upload.time', SimpleNamespace(monotonic=lambda: 1. + elapsed))
        await upload.measure()
        assert upload.error is None, upload.error
        assert upload.rate == pytest.approx(1000 / max(elapsed, 1e-9))
        await upload.close()
    asyncio.run(scenario())
