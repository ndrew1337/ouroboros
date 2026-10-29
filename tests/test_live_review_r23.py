"""Independent R23 transport-custody probes; synthetic data and local sockets only."""
import asyncio
import pytest

from tests._live_stack import stack
from tests.test_live_conversation import host_world as host_world
from tests.test_live_upload import screen, video_bytes
from tests._live_upload_wire import limit_connector, LimitedTransport
from tests.test_live_upload_controls import Session
from ouroboros.live_upload import WireMeter


@pytest.mark.parametrize('engine,thinking,rate', [('gemini',False,50_000),('gemini',True,50_000),('openai',False,80_000)])
@pytest.mark.parametrize('hidden', [False, True])
def test_unobservable_kernel_queue_is_not_link_throughput(host_world, monkeypatch, engine, thinking, rate, hidden):
    """Same bounded physical link; hide only the first socket-buffer worth of bytes."""
    async def scenario():
        if hidden:
            # Model writes accepted into a 256 KiB kernel/proxy buffer. The async
            # transport owns/reports only the overflow. No provider data is faked.
            monkeypatch.setattr(LimitedTransport, 'get_write_buffer_size',
                lambda self: max(0, self.queued - 256 * 1024) + self.native.get_write_buffer_size())
        monkeypatch.setattr(WireMeter, 'unsent', lambda self: None)
        monkeypatch.setattr(WireMeter, 'acknowledged', lambda self: None)
        links = limit_connector(monkeypatch, rate)
        original = screen()
        async with stack(host_world, monkeypatch, engine, thinking=thinking) as run:
            run.call.attention.mode = 'stream'
            await run.wait(lambda: len(run.wire.audio) > 9600)
            upload = run.call.session.upload
            measured = upload.rate
            run.page.video(original)
            await run.wait(lambda: any(video_bytes(v) is not None for v in run.wire.sent))
            size = next(len(video_bytes(v)) for v in run.wire.sent if video_bytes(v) is not None)
            observed = dict(engine=engine, thinking=thinking, hidden=hidden, physical_rate=rate,
                measured_rate=measured, original=len(original), delivered=size,
                wire_seconds=size * 4 / 3 / rate, queued=[v.queued for v in links])
            print('HIDDEN_QUEUE', observed)
            assert size * 4 / 3 <= rate * .5, observed
    asyncio.run(scenario())


@pytest.mark.parametrize('second_calibration', [False, True])
def test_opaque_calibration_arriving_during_fence_is_not_stranded(second_calibration):
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
        async def first_ping():
            while not pongs:
                await asyncio.sleep(0)
        await asyncio.wait_for(first_ping(), 2)
        if second_calibration:
            upload.note(3000, calibrate=True)
        pongs[0].set_result(None)
        for _ in range(5):
            await asyncio.sleep(0)
            for pong in pongs:
                if not pong.done(): pong.set_result(None)
        await asyncio.wait_for(upload.meter, 1)
        observed = dict(ack=upload.ack_bytes, fence=upload.fence_bytes, total=upload.total_bytes,
                        meter_done=upload.meter.done(), backlog=upload.backlog(), pongs=len(pongs))
        await upload.close()
        assert observed['backlog'] == 0, observed
    asyncio.run(scenario())


@pytest.mark.parametrize('engine,thinking', [('gemini',False),('gemini',True),('openai',False)])
def test_fully_opaque_fast_connection_keeps_frames(host_world, monkeypatch, engine, thinking):
    async def scenario():
        monkeypatch.setattr(WireMeter, 'unsent', lambda self: None)
        monkeypatch.setattr(WireMeter, 'acknowledged', lambda self: None)
        monkeypatch.setattr('ouroboros.live_upload.buffer_size', lambda _: None)
        limit_connector(monkeypatch, 10_000_000)
        async with stack(host_world, monkeypatch, engine, thinking=thinking) as run:
            run.call.attention.mode = 'stream'
            await run.wait(lambda: len(run.wire.audio) > 9600)
            original = screen()
            expected = []
            for number in range(6):
                frame = original[:2] + b'\xff\xfe\x00\x03' + bytes([number]) + original[2:]
                expected.append(frame)
                run.page.video(frame)
                await asyncio.sleep(1)
            await run.wait(lambda: len(run.wire.audio) > 96000)
            upload = run.call.session.upload
            facts = dict(ack=upload.ack_bytes, fence=upload.fence_bytes, total=upload.total_bytes,
                         rate=upload.rate, audio_rate=upload.audio_rate, backlog=upload.backlog())
            print('FULL_OPAQUE', engine, thinking, facts)
            assert [video_bytes(v) for v in run.wire.sent if video_bytes(v) is not None] == expected, facts
    asyncio.run(scenario())
