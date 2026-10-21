"""New canonical rows must fit before a driver forwards them to an open route."""
import asyncio
import queue
from dataclasses import replace

from ouroboros.contracts.live_conversation import LiveOptions
from tests.test_live_conversation import host_world as host_world


def test_pending_context_counts_before_and_after_receipts(host_world):
    host = host_world.host
    async def run():
        for producer in ('mailbox', 'peer', 'tool'):
            async def consume(session):
                # Leave ample room for this connection's initial selection, then
                # tighten the test window to 1,000 tokens of further growth.
                held = host._selected_raw_estimate
                host.model = replace(host.model, facts=replace(host.model.facts,
                    conversation_window_tokens=held + 11000, working_reserve_tokens=10000))
                assert host.renewal_due() is False
                if producer != 'peer':
                    host.response_done(producer, usage={'text_input_tokens': held}, raw_usage={}, status='completed')
                    assert host.renewal_due() is False
                async def add(text, ident):
                    if producer == 'mailbox':
                        host.incoming = queue.Queue()
                        host.incoming.put({'text': text, 'msg_id': ident})
                        assert await host.mailbox()
                    elif producer == 'peer':
                        await host.observe_context({'content': text})
                    else:
                        async def result(port, arguments):
                            return text
                        host.call_tools['set_test_language'] = replace(host.call_tools['set_test_language'], handler=result)
                        await host.tool_batch([{'id': ident, 'type': 'function',
                            'function': {'name': 'set_test_language', 'arguments': '{}'}}])
                await add('Fits inside the window.', producer + '-small')
                assert host.renewal_due() is False
                big = 'Pending canonical context. ' * 1200
                await add(big, producer + '-big')
                assert any(big in str(row.get('content', '')) for row in host.messages)
                assert host.renewal_due() is True
                # Renewal is a decision, not a mutation or silent context cut.
                assert any(big in str(row.get('content', '')) for row in host.messages)
            await host.session(consume, LiveOptions())
            host.model = replace(host.model, facts=replace(host.model.facts, conversation_window_tokens=200000))
    asyncio.run(run())
