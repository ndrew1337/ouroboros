"""Completed, interrupted and closing live receipts use the ordinary ledger."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ouroboros import usage_accounting as ua
from ouroboros.contracts.live_conversation import LiveOptions
from ouroboros.contracts.realtime import RealtimeSetupError
from tests.test_live_conversation import host_world as host_world
from tests.test_live_usage import token_usage


def tariff(**entry):
    return {'entry': entry, 'fetched_at': '2026-10-21T00:00:00+00:00', 'sha256': 'fixture-digest',
            'catalog_id': 'fixture', 'provider': 'fixture_live'}


def test_response_dedup_pinned_quote_and_shared_limit(host_world):
    host = host_world.host
    host.quote = tariff(input_cost_per_token=.001, output_cost_per_token=.002)
    with ua.usage_scope(ua.UsageScope(drive_root=host.ctx.drive_root, task_id=host.ctx.task_id, global_limit_usd=.01)):
        host.response_started('spoken')
        host.response_started('spoken')
        host.quote['entry']['input_cost_per_token'] = 100  # a refresh cannot reprice an open response
        for _ in range(2):
            host.response_done('spoken', usage=token_usage(text_input_tokens=12), raw_usage={'input_tokens': 12},
                               status='interrupted')
        row, = ua.read_usage_records(host.ctx.drive_root)
        assert row['cost_usd'] == pytest.approx(.012)
        assert row['cost_evidence']['status'] == 'interrupted'
        with pytest.raises(RealtimeSetupError, match='budget_exhausted'):
            host.response_started('next')
        host.response_started('next')  # duplicate notification creates no second refusal row
        for _ in range(2):
            host.response_done('next', usage=token_usage(text_input_tokens=1), raw_usage={}, status='interrupted')
    rows = ua.read_usage_records(host.ctx.drive_root)
    assert len(rows) == 2 and sum(row['cost_usd'] for row in rows) == pytest.approx(100.012)


def test_connected_watermarks_idle_tail_and_unknown_tariff_survive_close_error(host_world, monkeypatch):
    import ouroboros.live_conversation as live
    host = host_world.host
    host.quote = tariff(input_cost_per_second=.01)
    now = [1000.0]
    monkeypatch.setattr(live, 'time', SimpleNamespace(monotonic=lambda: now[0]))

    async def consume(session):
        host.response_started('tools')
        for seconds in (60, 60, 90, 90):
            host.response_done('tools', usage={'connected_seconds': seconds}, raw_usage={'fixture_meter': seconds},
                               status='interrupted')
        host.response_done('without-start', usage={'connected_seconds': 120}, raw_usage={}, status='completed')
        now[0] += 150
    asyncio.run(host.session(consume, LiveOptions()))
    rows = ua.read_usage_records(host.ctx.drive_root)
    assert len(rows) == 4
    assert sum(row['cost_usd'] for row in rows) == pytest.approx(1.5)
    assert sorted(row['cost_evidence']['usage']['connected_seconds'] for row in rows) == [30, 30, 30, 60]

    host.quote = None
    async def unknown(session):
        host.response_done('tools', usage={'connected_seconds': 10}, raw_usage={}, status='completed')
        now[0] += 20
        async def failed_close():
            raise RuntimeError('fixture close failed')
        session.close = failed_close
    with pytest.raises(RuntimeError, match='fixture close failed'):
        asyncio.run(host.session(unknown, LiveOptions()))
    added = ua.read_usage_records(host.ctx.drive_root)[4:]
    assert len(added) == 2 and all(row['cost_usd'] is None for row in added)
    assert [row['cost_evidence']['usage']['connected_seconds'] for row in added] == [10, 10]


def test_cancel_joins_started_tool_before_connection_closes(host_world):
    host = host_world.host
    checkpoints = []
    original = host.checkpoint
    def checkpoint(boundary):
        original(boundary)
        checkpoints.append(boundary)
    host.checkpoint = checkpoint

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def effect(host, arguments):
            entered.set()
            await release.wait()
            return 'Effect finished'
        host.call_tools['set_test_language'] = replace(host.call_tools['set_test_language'], handler=effect)
        call = {'id': 'slow-effect', 'type': 'function',
                'function': {'name': 'set_test_language', 'arguments': '{}'}}
        async def consume(session):
            await host.tool_batch([call])
        running = asyncio.create_task(host.session(consume, LiveOptions()))
        await entered.wait()
        running.cancel()
        await asyncio.sleep(0)
        assert not host_world.sessions[-1].closed and not running.done()
        running.cancel()  # repeated Stop still joins the same effect
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert host_world.sessions[-1].closed
        assert any(row.get('tool_call_id') == 'slow-effect' for row in host.messages)
        assert checkpoints == ['pre_effect', 'post_batch']
    asyncio.run(run())


def test_presence_caption_waits_for_store_and_keeps_participant_identity(host_world, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from ouroboros import dialogue_store
    from ouroboros.presence_call import record_call_utterance

    host = host_world.host
    task = {'id': 'presence-call', 'chat_id': host.turn.spec.chat_id, 'metadata': {'presence': {
        'binding_id': 'fixture-binding', 'transport_skill': 'fixture-transport', 'event': {
            'source_event_id': 'fixture-meeting', 'provider': 'fixture-meeting', 'account_id': 'fixture-account',
            'conversation_id': 'fixture-room', 'thread_id': '', 'conversation_key': 'fixture:meeting:room',
            'actor': {'display_name': 'Opening participant', 'platform_actor_id': 'opening-participant'}}}}}
    append, entered = dialogue_store.append, threading.Event()
    def signalled(*args, **kwargs):
        entered.set()
        return append(*args, **kwargs)
    monkeypatch.setattr(dialogue_store, 'append', signalled)
    root = host.ctx.drive_root
    # Initialize the real store before holding another writer's transaction.
    append(root, 'chat', {'direction': 'out', 'chat_id': task['chat_id'], 'text': 'Fixture setup'})
    with ThreadPoolExecutor(max_workers=1) as executor:
        with dialogue_store.transaction(root, write=True):
            pending = executor.submit(record_call_utterance, root, task, direction='in',
                text='Participant under contention', speaker_label='Meeting participant', utterance_id='heard-1')
            assert entered.wait(5)
            assert not pending.done()
        assert pending.result(timeout=5)
    assert record_call_utterance(root, task, direction='in', text='Participant under contention',
                                speaker_label='Meeting participant', utterance_id='heard-1')
    rows = [entry.row for entry in dialogue_store.iter_rows(root, 'chat') if entry.row.get('task_id') == task['id']]
    assert len(rows) == 1
    assert rows[0]['sender_label'] == 'Meeting participant'
    assert rows[0]['source'] == 'presence:fixture-meeting'
    assert rows[0]['client_message_id'] == 'fixture-meeting:heard:heard-1'
