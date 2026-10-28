"""Renewal and transport loss over the same gateway/host/native-wire harness."""
import asyncio
import json

import pytest

from tests._extension_loader_shared import _clear_loader_state  # noqa: F401
from tests._live_stack import stack
from tests.test_live_conversation import host_world as host_world


@pytest.mark.parametrize('engine,thinking', [('openai', False), ('grok', False), ('gemini', False), ('gemini', True)])
def test_full_stack_host_window_renews_with_pending_canonical_facts(host_world, monkeypatch, engine, thinking):
    async def scenario():
        async with stack(host_world, monkeypatch, engine, thinking=thinking) as run:
            run.wire.say('Synthetic answer before renewal.', complete=False)
            await run.wait(lambda: bool(run.call.active_responses))
            run.page.share(True, 'screen')
            await run.wait(lambda: any('started showing the screen' in str(r) for r in run.host.messages))
            before = len(run.provider.connections)
            if engine == 'grok':
                # The declared Grok window is an estimator window; token receipts
                # cannot change that basis. Add measured canonical text until the
                # real host growth calculation requests renewal.
                text = 'Synthetic pending room evidence. ' * 4500
                run.mail.put({'text': text, 'msg_id': 'window-growth'})
                await run.wait(lambda: any(text in str(r) for r in run.host.messages))
            else:
                run.wire.prompt = run.host.model.facts.conversation_window_tokens - run.host.model.facts.working_reserve_tokens + 1
            # Finish the native caption and receipt; page playback still gates quiet.
            if engine == 'gemini':
                run.wire.put({'serverContent': {'outputTranscription': {'text': ' Finished.', 'finished': True}}})
            else:
                run.wire.put({'type': 'response.output_audio_transcript.done',
                              'item_id': f'item-{run.wire.number}-{run.wire.responses}',
                              'transcript': 'Synthetic answer before renewal. Finished.'})
            run.wire.complete()
            await run.wait(lambda: len(run.provider.connections) > before)
            await run.quiet()
            assert any('started showing the screen' in str(v) for v in run.wire.sent), 'fresh fit must carry the pending fact'
            assert sum('started showing the screen' in str(r) for r in run.host.messages) == 1
            run.provider.script.append(('say', 'Synthetic conversation after renewal.'))
            await run.owner_says('Synthetic owner after renewal.')
            await run.quiet()
        assert run.result[1]['execution_status'] == 'ok', run.result
        assert any('Synthetic owner after renewal.' in str(r) for r in run.transcript())
    asyncio.run(scenario())


def test_full_stack_oversized_canonical_input_refuses_physically_instead_of_reconnecting_forever(host_world, monkeypatch):
    async def scenario():
        async with stack(host_world, monkeypatch, 'grok') as run:
            text = 'Synthetic pending room evidence. ' * 12000
            run.mail.put({'text': text, 'msg_id': 'physical-overflow'})
            await asyncio.wait_for(asyncio.shield(run.running), 15)
        assert run.result[1]['reason_code'] == 'context_capacity'
        assert run.result[1]['execution_status'] == 'infra_failed'
        assert any(text in str(r) for r in run.host.messages), 'physical refusal may not shorten owner words'
        assert len(run.provider.connections) == 1, 'overflow is refused before another provider connection'
        assert run.result[2]['fit_refusal']['deficit_tokens'] > 0
    asyncio.run(scenario())


@pytest.mark.parametrize('thinking', [False, True])
@pytest.mark.parametrize('reason', ['audio_drop', 'compression', 'go_away', 'transport_drop'])
def test_full_stack_gemini_retention_or_resume_preserves_conversation(host_world, monkeypatch, thinking, reason):
    async def scenario():
        async with stack(host_world, monkeypatch, 'gemini', thinking=thinking) as run:
            before = len(run.provider.connections)
            if reason in {'audio_drop', 'compression'}:
                run.wire.audio_tokens = 300
                if reason == 'compression':
                    # Intermediate provider sample crosses compression trigger;
                    # settled prompt is small, so the host itself need not renew.
                    run.wire.put({'usageMetadata': {'promptTokenCount': 92000}})
                run.wire.say('Synthetic before token fall.')
                await run.wait(lambda: run.call.session.held_audio == 300)
                await run.quiet()
                if reason == 'compression':
                    run.wire.put({'usageMetadata': {'promptTokenCount': 92000}})
                run.wire.audio_tokens = 50
                run.wire.say('Synthetic after token fall.')
                if reason == 'compression':
                    await run.wait(lambda: run.call.session.held_audio == 50)
                    await run.quiet()
                    assert len(run.provider.connections) == before
                    run.host.wire.flush()
                    assert any('compression_explains_drop' in r['raw'] for r in run.journal())
                else:
                    await run.wait(lambda: len(run.provider.connections) > before)
                    await run.quiet()
                    assert not run.wire.sent[0]['setup']['sessionResumption'], 'audio eviction needs a fresh retelling'
            else:
                run.wire.put({'sessionResumptionUpdate': {'resumable': True, 'newHandle': 'synthetic-resume-handle'}})
                await run.wait(lambda: run.host.state.get('resume_handle') == 'synthetic-resume-handle')
                if reason == 'transport_drop':
                    await run.wire.websocket.close(code=1012, reason='synthetic service restart')
                else:
                    run.wire.put({'goAway': {'timeLeft': '30s'}})
                await run.wait(lambda: len(run.provider.connections) > before)
                await run.quiet()
                assert run.wire.sent[0]['setup']['sessionResumption'] == {'handle': 'synthetic-resume-handle'}
                assert len([text for wire in run.provider.connections for text in wire.spoken
                            if text == 'Synthetic greeting heard.']) == 1
            run.provider.script.append(('say', 'Synthetic continued conversation.'))
            await run.owner_says('Synthetic owner still here.')
            await run.quiet()
        assert run.result[1]['execution_status'] == 'ok', run.result
        assert any('Synthetic owner still here.' in str(r) for r in run.transcript())
        assert any(json.loads(r['raw']).get('type') == 'call.offer_wait' for r in run.journal() if r['direction'] == 'host')
    asyncio.run(scenario())


@pytest.mark.parametrize('engine', ['openai', 'grok'])
def test_nonresumable_provider_drop_is_typed_and_does_not_buy_a_hidden_retry(host_world, monkeypatch, engine):
    async def scenario():
        async with stack(host_world, monkeypatch, engine) as run:
            await run.wire.websocket.close(code=1012, reason='synthetic service restart')
            await asyncio.wait_for(asyncio.shield(run.running), 10)
        assert run.result[1]['reason_code'] == 'provider_closed'
        assert run.result[1]['execution_status'] == 'infra_failed'
        assert len(run.provider.connections) == 1
        assert any('Synthetic greeting heard.' in str(r) for r in run.transcript())
        assert run.journal()
    asyncio.run(scenario())
