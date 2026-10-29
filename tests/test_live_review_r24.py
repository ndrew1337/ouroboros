"""Cold-start fidelity and offer custody, without a throwaway calibration image."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from tests.test_live_upload import screen, video_bytes
from tests._live_upload_wire import limit_connector
from tests._live_stack import stack
from tests.test_live_conversation import host_world as host_world

@pytest.mark.parametrize('interrupted',[False,True])
def test_reserved_offer_image_survives_intervening_speech(interrupted):
    import base64
    from ouroboros.realtime_session import JsonSession
    from ouroboros.contracts.live_conversation import Video, ContextUpdate
    from skills.live_conversation.attention import Attention
    from skills.live_conversation.driver import _Call
    async def scenario():
        class Session(JsonSession):
            def video_payload(self,frame): return {'image':base64.b64encode(frame.data).decode()}
            async def send(self,command):
                if isinstance(command,Video):
                    await self.write(self.video_payload(command),image=True)
                elif isinstance(command,ContextUpdate):
                    await self.write({'context':list(command.messages)})
        sent=[]
        class Socket:
            transport=SimpleNamespace(get_write_buffer_size=lambda:0,get_extra_info=lambda *_:None)
            async def send(self,raw): sent.append(json.loads(raw))
        session=Session(None,None,connect=None,observe=lambda *a:None)
        session.ws=Socket()
        session.upload.rate=10_000_000
        host=SimpleNamespace(model=SimpleNamespace(vision=True),renewal_due=lambda:False,state={})
        attention=Attention(host)
        original=screen()
        frame=Video(original,'image/jpeg','screen')
        attention.frames['screen']=frame
        rows=[{'role':'user','_call_frame_source':'screen','_call_frame_digest':'synthetic',
               'content':[{'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+base64.b64encode(original).decode()}}]}]
        call=object.__new__(_Call)
        call.host,call.session,call.attention=host,session,attention
        call.pending_offers=rows
        call.connected=asyncio.Event(); call.connected.set()
        checks=[0]
        def quiet():
            checks[0]+=1
            return not (interrupted and checks[0]==2)
        call.quiet=quiet
        await call.offer_pending()
        if interrupted:
            assert not sent
            await call.offer_pending()
        for _ in range(10): await asyncio.sleep(.01)
        evidence=dict(interrupted=interrupted,slot=getattr(session.upload,'slot',None) is not None,
            pending=list(session.upload.pending),sent_kinds=[list(v) for v in sent],
            has_pixels=any('image' in v or 'data:image/jpeg;base64,' in json.dumps(v) for v in sent))
        await session.close()
        assert evidence['has_pixels'] and not evidence['slot'],evidence
    asyncio.run(scenario())


@pytest.mark.parametrize('engine,thinking', [('gemini', False), ('gemini', True), ('openai', False)])
@pytest.mark.parametrize('measurement', ['native', 'no_kernel', 'hidden', 'opaque'])
@pytest.mark.parametrize('requested', [False, True])
def test_good_link_first_image_is_original_and_second_look_is_truthful(
        host_world, monkeypatch, engine, thinking, measurement, requested):
    from tests.test_live_upload import measurement_arm
    async def scenario():
        measurement_arm(monkeypatch, measurement)
        limit_connector(monkeypatch, 10_000_000)
        original = screen()
        async with stack(host_world, monkeypatch, engine, thinking=thinking) as run:
            run.call.attention.mode = 'asked' if requested else 'stream'
            # No bootstrap/warm-up image: the setup's measured line is available
            # before the owner starts sharing; this is the FIRST actual image.
            await run.wait(lambda: len(run.wire.audio) > 9600)
            run.page.video(original)
            await run.wait(lambda: bool(run.call.attention.frames))
            if requested:
                await run.tool('first-look', 'attend', {'look': True})
                await run.wait(lambda: 'first-look' in run.wire.outputs)
            await run.wait(lambda: any(video_bytes(v) is not None for v in run.wire.sent))
            first = [video_bytes(v) for v in run.wire.sent if video_bytes(v) is not None]
            evidence = dict(engine=engine, thinking=thinking, measurement=measurement,
                            requested=requested, source=len(original), first=list(map(len, first)))
            print('FIRST_IMAGE', evidence)
            assert first == [original], evidence
            await run.quiet()
            await run.tool('same-look', 'attend', {'look': True})
            await run.wait(lambda: 'same-look' in run.wire.outputs)
            await run.quiet()
            text = run.wire.outputs['same-look']
            if isinstance(text, dict):
                text = text['output']
            result = json.loads(text)
            assert result['frames'][0]['already_visible'] is True, result
            assert [video_bytes(v) for v in run.wire.sent if video_bytes(v) is not None] == [original]
            from pathlib import Path
            assert Path(result['frames'][0]['file_path']).read_bytes() == original
            from ouroboros.live_upload import SLOW, RECOVERED
            assert not any(SLOW in str(r) or RECOVERED in str(r) for r in run.host.messages)
        assert run.result[1]['execution_status'] == 'ok'
    asyncio.run(scenario())
