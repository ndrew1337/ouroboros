"""A completed call result gets a fresh full view before an ordinary range fallback."""
import asyncio
import json

import pytest

from ouroboros.contracts.live_conversation import LiveOptions, ToolOutputs
from ouroboros.contracts.realtime import RealtimeSetupError
from tests.test_live_conversation import host_world as host_world


@pytest.mark.parametrize('large,failed,reader_loaded,annotated', [
    (False,False,False,False),(True,False,False,False),
    (True,True,False,False),(True,True,True,False),
    (True,True,False,True),(True,True,True,True)])
def test_call_result_refits_whole_then_falls_back_without_repeating_effect(
        host_world, monkeypatch, large, failed, reader_loaded, annotated):
    from skills.live_conversation.driver import _Call
    from ouroboros.tools.tool_result import ToolResult
    host=host_world.host
    host.schemas[:] = [schema for schema in host.schemas if schema['function']['name'] != 'read_file']
    if reader_loaded:
        host.schemas.extend(schema for schema in host.tools.schemas() if schema['function']['name'] == 'read_file')
    prepare = host.prepare_session
    execute_result = host.tools.execute_result
    body='Result from one completed effect. ' * (1600 if large else 1)
    effects=[]
    def execute(name,args):
        effects.append(name)
        return ToolResult(status='ok',code='OK',text=body + ('\nHost annotation.' if annotated else ''),
                          producer_text=body if annotated else None,
                          host_annotations=('Host annotation.',) if annotated else ())
    monkeypatch.setattr(host.tools, 'execute_result', execute)
    async def consume(session):
        host._held_input=host._renewal_window.total_equivalent-host._renewal_window.reserve-1200
        host._history_at_receipt=host._history_estimate()
        call=_Call(host); call.session=session; call.connected.set()
        sent=[]
        async def send(command): sent.append(command)
        session.send=send
        if failed:
            async def refuse(options):
                error=RealtimeSetupError('context_capacity','Irreducible view')
                error.source_revision=host._history_version
                raise error
            monkeypatch.setattr(host,'prepare_session',refuse)
        await call.tool_queue.put({'id':'one','type':'function','function':{'name':'list_available_tools','arguments':'{}'}})
        worker=asyncio.create_task(call.tools())
        try:
            await asyncio.wait_for(call.tool_queue.join(),30)
            canonical=next(row for row in host.messages if row.get('tool_call_id')=='one')
            assert body in canonical['content'] and effects==['list_available_tools']
            if not large:
                assert call.preparing is None and any(isinstance(c,ToolOutputs) and body in str(c.messages) for c in sent)
                return
            assert not sent and call.deferred_tool_outputs, 'never send the empty first-show view before the fresh fit'
            if failed:
                await asyncio.gather(call.preparing,return_exceptions=True)
                await call.check_preparation()
                output=next(c for c in sent if isinstance(c,ToolOutputs))
                text=next(r['content'] for r in output.messages if r.get('tool_call_id')=='one')
                assert 'RESULT_VIEW_JSON=' in text and body not in text
                view = json.loads(next(line.removeprefix('RESULT_VIEW_JSON=') for line in text.splitlines()
                                       if line.startswith('RESULT_VIEW_JSON=')))
                source = json.loads(next(line.removeprefix('FULL_RESULT_SOURCE_JSON=') for line in text.splitlines()
                                         if line.startswith('FULL_RESULT_SOURCE_JSON=')))
                assert bool(view.get('read_omitted')) is reader_loaded
                assert bool(source.get('read')) is reader_loaded
                if annotated:
                    producer = json.loads(next(line.removeprefix('PRODUCER_RESULT_SOURCE_JSON=') for line in text.splitlines()
                                               if line.startswith('PRODUCER_RESULT_SOURCE_JSON=')))
                    assert bool(producer.get('read')) is reader_loaded
                    assert 'Host annotation.' in text
                if reader_loaded:
                    ref = view['read_omitted']
                    recovered = execute_result('read_file', {key:value for key,value in ref.items() if key != 'tool'})
                    assert recovered.status == 'ok'
                    assert body[ref['start_char']:ref['start_char']+ref['max_chars']] in recovered.text
                else:
                    assert 'no reader is loaded' in text
                assert not call.deferred_tool_outputs
                assert body in canonical['content']
                monkeypatch.setattr(host, 'prepare_session', prepare)
            # Adopt successive real fitted prefixes, including recovery after a
            # range fallback. Later renewals must never inherit that wire view.
            for renewal in range(4):
                lease = await call.preparing if renewal == 0 and not failed else await prepare(LiveOptions())
                async def adopted(session):
                    rows = json.loads(session.selected.messages_json)
                    result = next(row for row in rows if row.get('tool_call_id') == 'one')
                    assert result['content'] == canonical['content']
                    assert body in result['content'] and 'RESULT_VIEW_JSON=' not in result['content']
                    assert next(row for row in host.messages if row.get('tool_call_id') == 'one') == canonical
                try:
                    await lease.run(adopted)
                finally: await lease.close()
            assert effects==['list_available_tools']
        finally:
            worker.cancel(); await asyncio.gather(worker,return_exceptions=True)
            await call.close_preparation()
    asyncio.run(host.session(consume,LiveOptions()))
