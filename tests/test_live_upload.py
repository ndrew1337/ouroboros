"""Microphone-first provider uploads, with real gateway/host/adapters on loopback."""
import asyncio
import base64
import io
import json
import time

import pytest
from PIL import Image

from ouroboros.contracts.live_conversation import Video
from ouroboros.live_upload import smaller_frame, SLOW, RECOVERED
from tests.test_live_conversation import host_world as host_world
from tests._live_stack import stack
from tests._live_upload_wire import limit_connector


def screen():
    # Deterministic, detailed synthetic 1280px source, not any operator's display.
    import random
    randomizer = random.Random(25)
    pixels = bytes(randomizer.randrange(256) for _ in range(1280 * 720))
    image = Image.frombytes('L', (1280, 720), pixels).convert('RGB')
    output = io.BytesIO()
    image.save(output, format='JPEG', quality=40)
    return output.getvalue()


def pending_page_audio(page):
    from ouroboros import call_protocol as cp
    frames = (cp.decode_frame(v['bytes']) for v in page.incoming._queue if v.get('bytes'))
    return sum(len(f.pcm) for f in frames if isinstance(f, cp.AudioFrame) and f.kind == cp.FRAME_AUDIO_IN)


def video_bytes(event):
    video = event.get('realtimeInput', {}).get('video')
    if video:
        return base64.b64decode(video['data'])
    for part in event.get('item', {}).get('content', []):
        if part.get('type') == 'input_image' and part.get('image_url'):
            return base64.b64decode(part['image_url'].split(',', 1)[1])
    return None


@pytest.mark.parametrize('metadata', [False, True])
def test_delivery_copy_obeys_bytes_and_preserves_original(metadata):
    data = screen()
    if metadata:
        data = data[:2] + b'\xff\xfe' + (60002).to_bytes(2, 'big') + b'x' * 60000 + data[2:]
    original = Video(data, 'image/jpeg', 'screen')
    assert smaller_frame(original, len(original.data)) is original
    copy = smaller_frame(original, 3000)
    assert copy is not None and len(copy.data) <= 3000 and copy.source == original.source
    assert Image.open(io.BytesIO(copy.data)).width < 1280
    assert smaller_frame(original, 100) is None
    assert Image.open(io.BytesIO(original.data)).width == 1280


def measurement_arm(monkeypatch, measurement):
    from tests._live_upload_wire import LimitedTransport
    if measurement != 'native':
        from ouroboros.live_upload import WireMeter
        monkeypatch.setattr(WireMeter, 'unsent', lambda _: None)
        if measurement != 'ack_only':
            monkeypatch.setattr(WireMeter, 'acknowledged', lambda _: None)
    if measurement == 'hidden':
        monkeypatch.setattr(LimitedTransport, 'get_write_buffer_size',
            lambda self: max(0, self.queued - 256 * 1024) + self.native.get_write_buffer_size())
    if measurement == 'opaque':
        # All three unavailable: keeping this accessor would miss the fixed pong fallback.
        monkeypatch.setattr('ouroboros.live_upload.buffer_size', lambda _: None)


@pytest.mark.parametrize('engine,thinking,rate', [('gemini', False, 50_000), ('gemini', True, 50_000), ('openai', False, 80_000)])
@pytest.mark.parametrize('measurement', ['native', 'no_kernel', 'ack_only', 'hidden', 'opaque'])
def test_minute_sharing_keeps_microphone_and_reports_recovery(host_world, monkeypatch, engine, thinking, rate, measurement):
    measurement_arm(monkeypatch, measurement)
    asyncio.run(sharing(host_world, monkeypatch, engine, rate, thinking=thinking, measurement=measurement))


async def sharing(host_world, monkeypatch, engine, rate, seconds=60, *, thinking=False, measurement="native"):
    links = limit_connector(monkeypatch, rate)
    original = screen()
    audio_times, sizes = [], []
    from tests._live_stack_wire import NativeWire
    native_send = NativeWire.send
    async def observe(wire, raw):
        event = json.loads(raw)
        if event.get('type') == 'input_audio_buffer.append' or 'audio' in event.get('realtimeInput', {}):
            audio_times.append(time.monotonic())
        image = video_bytes(event)
        if image is not None:
            sizes.append(len(image))
        await native_send(wire, raw)
    monkeypatch.setattr(NativeWire, 'send', observe)
    async with stack(host_world, monkeypatch, engine, thinking=thinking) as run:
        run.call.attention.mode = 'stream'
        run.page.send({'type':'share', 'sharing':True, 'kind':'screen'})
        # Continuous page-rate input, including silence, with the page's actual
        # one-second unsent-audio failure bound (the ASGI queue is its fake socket).
        tick = run.page.tick
        unsent_max, failures = [0], []
        def capture_tick(*, capture=True):
            unsent = pending_page_audio(run.page)
            unsent_max[0] = max(unsent_max[0], unsent)
            if unsent > 48000 * 2 and not failures:
                failures.append('slow_uplink')
                run.page.send({'type':'bye', 'reason':'failed'})
            if not failures:
                tick(capture=capture)
        monkeypatch.setattr(run.page, 'tick', capture_tick)
        await run.wait(lambda: len(audio_times) >= 10)
        start = time.monotonic()
        first_audio = len(audio_times)
        for number in range(seconds):
            # JPEG comments change exact bytes while keeping the synthetic source geometry.
            comment = str(number).encode()
            run.page.video(original[:2] + b'\xff\xfe' + (len(comment)+2).to_bytes(2,'big') + comment + original[2:])
            await asyncio.sleep(max(0, start + number + 1 - time.monotonic()))
        assert not failures, failures
        assert time.monotonic() - audio_times[-1] < 1, ([v for v in run.page.controls if v.get('type') == 'state'], run.call.renewing, run.call.provider_closed, run.call.quiet_blockers())
        assert not run.running.done(), run.result if hasattr(run, 'result') else run.page.closed
        gaps = [b-a for a,b in zip(audio_times[first_audio:], audio_times[first_audio+1:])]
        print('UPLOAD_PRESSURE', engine, dict(thinking=thinking, measurement=measurement, rate=rate,
              sizes=list(sizes), max_audio_gap=max(gaps) if gaps else None))
        assert gaps and max(gaps) < 1, (engine, max(gaps), sizes)
        assert 0 < len(sizes) < seconds and max(sizes) < len(original), (sizes, run.call.session.upload.rate, run.call.session.upload.audio_rate, run.call.session.upload.budget())
        facts = [r.get('content','') for r in run.host.messages]
        assert sum(SLOW in str(r) for r in facts) == 1, facts
        before = len(sizes)
        slow_sizes = list(sizes)
        for link in links:
            link.rate = 10_000_000
        # Keep the page's changing source at its actual cadence throughout the
        # recovery observation. Stopping after ten captures can strand the last
        # reduced copy just before its receipt learns the recovered capacity.
        async def recovering():
            import itertools
            for number in itertools.count():
                comment = str(number).encode()
                recovery = original[:2] + b'\xff\xfe' + (len(comment)+2).to_bytes(2, 'big') + comment + original[2:]
                run.page.video(recovery)
                await asyncio.sleep(1)
                if len(sizes) > before and sizes[-1] == len(recovery):
                    return
        print('UPLOAD_SLOW', engine, dict(thinking=thinking, measurement=measurement, rate=rate,
              sizes=slow_sizes, max_audio_gap=max(gaps)))
        try:
            await asyncio.wait_for(recovering(), 120)  # the harness's existing hang bound
        finally:
            print('RECOVERY', engine, dict(rate=run.call.session.upload.rate, audio_rate=run.call.session.upload.audio_rate,
                  backlog=run.call.session.upload.backlog(), budget=run.call.session.upload.budget(), sizes=sizes))
        await run.wait(lambda: sum(RECOVERED in str(r) for r in run.host.messages) == 1)
        assert sum(SLOW in str(r) for r in run.host.messages) == 1
        print('UPLOAD', engine, dict(seconds=seconds, thinking=thinking, measurement=measurement, rate=rate, source_bytes=len(original), slow_sizes=slow_sizes,
              delivered_frames=len(sizes), sizes=sizes, audio_wire_rate=run.call.session.upload.audio_rate,
              max_audio_gap=max(gaps), max_page_unsent_audio_seconds=unsent_max[0] / 96000))
    assert run.result[1]['execution_status'] == 'ok', run.result


@pytest.mark.parametrize('engine', ['gemini', 'openai'])
@pytest.mark.parametrize('measurement', ['native', 'no_kernel', 'ack_only'])
def test_fast_upload_keeps_full_frame_and_stays_quiet(host_world, monkeypatch, engine, measurement):
    if measurement != 'native':
        from ouroboros.live_upload import WireMeter
        monkeypatch.setattr(WireMeter, 'unsent', lambda self: None)
        if measurement == 'no_kernel':
            monkeypatch.setattr(WireMeter, 'acknowledged', lambda self: None)
    async def scenario():
        limit_connector(monkeypatch, 10_000_000)
        original = screen()
        async with stack(host_world, monkeypatch, engine) as run:
            run.call.attention.mode = 'stream'
            await run.wait(lambda: run.call.session.upload.capacity() > len(original) * 4 / 3 + 1024)
            run.page.video(original)
            await run.wait(lambda: any(video_bytes(v) == original for v in run.wire.sent))
            assert not any(SLOW in str(r) or RECOVERED in str(r) for r in run.host.messages)
        assert run.result[1]['execution_status'] == 'ok'
    asyncio.run(scenario())


@pytest.mark.parametrize('engine,rate', [('gemini',30_000), ('openai',50_000), ('grok',50_000)])
def test_link_below_microphone_rate_still_reaches_page_slow_uplink(host_world, monkeypatch, engine, rate):
    async def scenario():
        limit_connector(monkeypatch, rate)
        async with stack(host_world, monkeypatch, engine) as run:
            tick = run.page.tick
            failures = []
            def page_tick(*, capture=True):
                unsent = pending_page_audio(run.page)
                if unsent > 48000 * 2 and not failures:
                    failures.append('slow_uplink')  # call.js's exact one-second PCM check
                    run.page.send({'type':'bye', 'reason':'failed'})
                if not failures:
                    tick(capture=capture)
            monkeypatch.setattr(run.page, 'tick', page_tick)
            await run.wait(lambda: bool(failures))
            assert failures == ['slow_uplink']
            assert not any(video_bytes(v) for v in run.wire.sent)
        assert run.result[1]['execution_status'] == 'ok'  # the page reports failed/slow_uplink and sends bye
    asyncio.run(scenario())


def test_recorded_1da43d33_frame_order_does_not_stall_microphone(host_world, monkeypatch):
    """All five video deliveries from the copied journal, synthetic pixels only.

    Original JPEG bytes and relative times below are copied measurements, not pixels.
    The owner's recorded outgoing microphone gaps reached 6.248 and 8.036 s.
    Recreate the offered order, not those already-stalled microphone timestamps.
    """
    async def scenario():
        limit_connector(monkeypatch, 50_000)
        from tests.test_live_proactive_host import synthetic_jpeg
        from tests._live_stack_wire import NativeWire
        original = synthetic_jpeg()
        times = []
        native = NativeWire.send
        async def observed(wire, raw):
            if 'audio' in json.loads(raw).get('realtimeInput', {}):
                times.append(time.monotonic())
            await native(wire, raw)
        monkeypatch.setattr(NativeWire, 'send', observed)
        async with stack(host_world, monkeypatch, 'gemini') as run:
            run.call.attention.mode = 'stream'
            await run.wait(lambda: len(times) >= 10)
            start = time.monotonic()
            for offset, target in [(0,132598), (2.027,134005), (4.001,133896), (24.552,138640), (36.596,97358)]:
                await asyncio.sleep(max(0, start + offset - time.monotonic()))
                # JPEG comments reproduce wire pressure without retaining owner screen content.
                frame = original
                while len(frame) < target:
                    count = min(65533, target-len(frame)-4)
                    if count < 0:
                        break
                    frame = frame[:2] + b'\xff\xfe' + (count+2).to_bytes(2,'big') + b'x'*count + frame[2:]
                run.page.video(frame)
            await asyncio.sleep(4)
            gaps = [b-a for a,b in zip(times,times[1:]) if a >= start]
            assert max(gaps) < 1, ('recorded upload order stalled live microphone', max(gaps))
            assert any(video_bytes(v) for v in run.wire.sent)
            print('REPLAY_1da43d33 max_audio_gap', max(gaps))
    asyncio.run(scenario())
