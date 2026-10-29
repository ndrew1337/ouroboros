"""Missing upload fences use the existing heartbeat and transport-loss continuation."""
import asyncio

import pytest
from websockets.frames import Opcode

from tests.test_live_conversation import host_world as host_world
from tests._live_stack import stack
from tests.test_live_upload import screen, video_bytes


@pytest.mark.parametrize('engine,thinking', [('gemini', False), ('gemini', True), ('openai', False)])
@pytest.mark.parametrize('recover', [False, True])
def test_pong_loss_recovers_frames_or_continues_call_after_native_heartbeat(
        host_world, monkeypatch, engine, thinking, recover):
    from ouroboros.live_upload import WireMeter
    from websockets.server import ServerProtocol
    async def scenario():
        # Force the actual pong fence, with all local queue/ACK access unavailable.
        monkeypatch.setattr(WireMeter, 'unsent', lambda _: None)
        monkeypatch.setattr(WireMeter, 'acknowledged', lambda _: None)
        monkeypatch.setattr('ouroboros.live_upload.buffer_size', lambda _: None)
        async with stack(host_world, monkeypatch, engine, thinking=thinking) as run:
            run.call.attention.mode = 'stream'
            await run.wait(lambda: run.call.session.upload.rate is not None)
            old_wire, old_session = run.wire, run.call.session
            await run.owner_says('Owner words retained across a missing pong.')
            await run.quiet()
            drop = [True]
            send_frame = ServerProtocol.send_frame
            def filtered(protocol, frame):
                if protocol is old_wire.websocket.protocol and frame.opcode == Opcode.PONG and drop[0]:
                    return
                send_frame(protocol, frame)
            monkeypatch.setattr(ServerProtocol, 'send_frame', filtered)
            original = screen()
            run.page.video(original)
            await run.wait(lambda: any(video_bytes(v) is not None for v in old_wire.sent))
            await run.wait(lambda: old_session.upload.meter is not None and not old_session.upload.meter.done())
            next_frame = original[:2] + b'\xff\xfe\x00\x03x' + original[2:]
            run.page.video(next_frame)
            if recover:
                drop[0] = False
                # The next native heartbeat pong acknowledges older prefixes too.
                await run.wait(lambda: sum(video_bytes(v) is not None for v in old_wire.sent) >= 2)
                # A delayed receipt can reduce the next copy. The contract here
                # is resumed pixels on the SAME connection, not a fixed number
                # of samples until full resolution (the cold-start matrix pins originals).
                frames = [video_bytes(v) for v in old_wire.sent if video_bytes(v) is not None]
                assert frames[0] == original and frames[1]
                print('PONG_RECOVERY', engine, thinking, list(map(len, frames)))
                assert len(run.provider.connections) == 1
            else:
                # Real native 20s interval + 20s timeout, no test/production replacement timer.
                assert old_session.ws.ping_interval == old_session.ws.ping_timeout == 20
                await run.wait(lambda: len(run.provider.connections) == 2 and run.call.connected.is_set())
                await run.quiet()
                assert any('Owner words retained across a missing pong.' in str(v) for v in run.wire.sent)
                assert any(v.get('type') == 'call.renewal' for v in run.page.controls) or run.host.connection_number == 2
                run.page.video(next_frame)
                await run.wait(lambda: any(video_bytes(v) == next_frame for v in run.wire.sent))
                assert sum(s == 'Synthetic greeting heard.' for w in run.provider.connections for s in w.spoken) == 1
            assert not run.page.closed
        assert run.result[1]['execution_status'] == 'ok', run.result
    asyncio.run(scenario())
