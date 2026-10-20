"""Isolated consumers only. No credentials, provider sockets or device capture.

Run through scripts/safe_test.py. Echoes prove our consumers, not vendor acceptance.
Raw wire/ledger receipts are exported even when assertions fail.
"""
from __future__ import annotations
import asyncio
import base64
import importlib
import json
import os
import queue
import shutil
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests._extension_loader_shared import _clear_loader_state  # noqa: F401
from tests.test_realtime_turn import wait_for

ROOT = Path(__file__).resolve().parents[1]
ENGINES = [('openai_realtime', 'openai'), ('grok_realtime', 'grok'), ('gemini_live', 'gemini')]


@pytest.fixture
def packages(tmp_path):
    from ouroboros import extension_loader
    from ouroboros.skill_loader import (find_skill, save_enabled, SkillReviewState,
                                       requested_skill_permissions, save_skill_grants)
    drive = tmp_path / 'packages'
    found = {}
    for name in ['live_conversation', *(name for name, _ in ENGINES)]:
        directory = drive / 'skills/external' / name
        shutil.copytree(ROOT / 'skills' / name, directory)
        (directory / 'late_identity.py').write_text('VALUE = "synthetic-old-import"\n')
        skill = find_skill(drive, name)
        save_enabled(drive, name, True)
        skill = replace(skill, enabled=True, manifest=replace(skill.manifest, env_from_settings=[]),
                        review=SkillReviewState(status='pass', content_hash=skill.content_hash))
        permissions = requested_skill_permissions(skill.manifest.permissions, skill.manifest.subscribe_events)
        save_skill_grants(drive, name, [], content_hash=skill.content_hash, requested_keys=[],
                          granted_permissions=permissions, requested_permissions=permissions)
        assert extension_loader.load_extension(skill, lambda: {}, drive_root=drive) is None
        found[name] = skill
    yield drive, found
    for name in found:
        extension_loader.unload_extension(name)


class VoiceWire:
    """Synthetic vendor-dialect fixture with one ordinary read_file tool call."""
    def __init__(self, engine, path):
        self.engine, self.path = engine, path
        self.sent, self.received = [], []
        self.events = asyncio.Queue()
        self.session = {}
        self.invoked = False
        self.result_received = threading.Event()
        self.closed = False
        self.closing = threading.Event()
        self.allow_close = threading.Event()
        self.allow_close.set()
        self.after_close = lambda: None

    def put(self, value):
        self.events.put_nowait(value)

    async def recv(self):
        value = await self.events.get()
        self.received.append(value)
        return json.dumps(value)

    def start_tool(self):
        if self.invoked:
            return
        self.invoked = True
        args = {'path': str(self.path)}
        if self.engine == 'gemini':
            self.put({'serverContent': {'inputTranscription': {'text': 'Read the synthetic canary.', 'finished': True}}})
            self.put({'toolCall': {'functionCalls': [{'id': 'read-1', 'name': 'read_file', 'args': args}]}})
        else:
            self.put({'type': 'conversation.item.input_audio_transcription.completed', 'item_id': 'owner-1',
                      'transcript': 'Read the synthetic canary.', 'status': 'completed'})
            self.put({'type': 'response.created', 'response': {'id': 'tools-1'}})
            self.put({'type': 'response.done', 'usage': {'input_audio_duration': 2}, 'response': {
                'id': 'tools-1', 'status': 'completed', 'usage': {'input_tokens': 100, 'output_tokens': 5},
                'output': [{'type': 'function_call', 'call_id': 'read-1', 'name': 'read_file',
                            'arguments': json.dumps(args)}]}})

    def answer(self):
        pcm = base64.b64encode(b'\x00\x00' * 480).decode()
        if self.engine == 'gemini':
            self.put({'serverContent': {'modelTurn': {'parts': [{'inlineData': {
                'mimeType': 'audio/pcm;rate=24000', 'data': pcm}}]},
                'outputTranscription': {'text': 'Synthetic canary read.'}}})
            self.put({'usageMetadata': {'promptTokenCount': 120, 'responseTokenCount': 12},
                      'serverContent': {'turnComplete': True}})
        else:
            self.put({'type': 'response.created', 'response': {'id': 'answer-1'}})
            self.put({'type': 'response.output_audio.delta', 'item_id': 'answer-item', 'delta': pcm})
            self.put({'type': 'response.output_audio_transcript.done', 'item_id': 'answer-item',
                      'transcript': 'Synthetic canary read.'})
            self.put({'type': 'response.done', 'usage': {'input_audio_duration': 3}, 'response': {
                'id': 'answer-1', 'status': 'completed', 'usage': {'input_tokens': 120, 'output_tokens': 12}, 'output': []}})

    async def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        kind = event.get('type')
        if 'setup' in event:
            self.put({'setupComplete': {}})
        elif kind == 'session.update':
            for key, value in event['session'].items():
                if key == 'audio' and key in self.session:
                    for direction, fields in value.items():
                        self.session['audio'].setdefault(direction, {}).update(fields)
                else:
                    self.session[key] = value
            self.put({'type': 'session.updated', 'session': self.session.copy()})
        elif kind == 'conversation.item.create':
            self.put({'type': 'conversation.item.done', 'item': event['item'],
                      'previous_item_id': event.get('previous_item_id')})
            if event['item']['type'] == 'function_call_output':
                self.result_received.set()
        elif kind == 'response.create':
            self.answer()
        elif kind == 'input_audio_buffer.append' or 'audio' in event.get('realtimeInput', {}):
            self.start_tool()
        elif 'toolResponse' in event:
            self.result_received.set()
            self.answer()
        elif kind == 'conversation.item.truncate':
            self.put({'type': 'conversation.item.truncated', 'item_id': event['item_id'], 'audio_end_ms': event['audio_end_ms']})

    @asynccontextmanager
    async def connect(self, model):
        try:
            yield self
        finally:
            self.closing.set()
            await asyncio.to_thread(self.allow_close.wait, 10)
            self.after_close()
            self.closed = True


def test_separate_loaded_registrations_and_beta_default(packages):
    from ouroboros.live_conversation import feature_configuration
    from ouroboros.realtime_setup import loaded_provider
    with feature_configuration('live_conversation:call') as (_, config):
        assert config['enabled'] is False and not config['provider']
    routes = [loaded_provider(f'{name}:{engine}')['route'] for name, engine in ENGINES]
    assert len({route.accounting_provider for route in routes}) == 3
    assert len({route.endpoint for route in routes}) == 3
    assert all(callable(loaded_provider(f'{name}:{engine}')['session']) for name, engine in ENGINES)


@pytest.mark.parametrize('name,engine', ENGINES)
@pytest.mark.parametrize('ending', ['bye', 'owner_stop'])
def test_full_consumer_memory_tools_history_money_stop(packages, tmp_path, monkeypatch, provider_catalog_offline, name, engine, ending):
    from ouroboros import config, context, call_protocol as cp, usage_accounting as ua
    from ouroboros.call_peers import open_owner_hub, close_hub
    from ouroboros.live_conversation import LiveConversationTurn, loaded_feature
    from ouroboros.extension_registry_state import _realtime_providers
    from ouroboros.model_wait import task_model_wait_scope
    from ouroboros.task_results import write_task_result, STATUS_RUNNING
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.tools.tool_context import ToolContext
    from tests._memory_view_context import world
    from tests.test_budget_fence_liveness import _publish

    env, memory, rooms = world(tmp_path / 'world')
    monkeypatch.setattr(config, 'DATA_DIR', env.drive_root)
    monkeypatch.setenv('OUROBOROS_DATA_DIR', str(env.drive_root))
    monkeypatch.setattr(context, 'get_context_mode', lambda: 'nano')
    from supervisor import message_bus
    for attribute in ('DATA_DIR', 'TOTAL_BUDGET_LIMIT', 'BUDGET_REPORT_EVERY_MESSAGES', '_BRIDGE'):
        monkeypatch.setattr(message_bus, attribute, getattr(message_bus, attribute))
    message_bus.init(env.drive_root, 10, 100, message_bus.LocalChatBridge(settings={}))
    canary = env.repo_dir / 'synthetic-call-canary.txt'
    canary.write_text('SYNTHETIC-CALL-CANARY-ONLY')
    wire = VoiceWire(engine, canary)
    provider_key = f'{name}:{engine}'
    factory = _realtime_providers[provider_key]['session']
    selected_inputs = []
    def captured_session(selected, options, *, observe):
        from dataclasses import asdict
        selected_inputs.append(asdict(selected))
        return factory(selected, options, observe=observe, connect=wire.connect)
    monkeypatch.setitem(_realtime_providers[provider_key], 'session', captured_session)
    events, results, failures = [], [], []
    spec = cp.CallSpec('synthetic-' + engine, rooms['alpha'], provider_key, 'synthetic-live-model', '', client_message_id='synthetic-input')
    turn = LiveConversationTurn(loaded_feature('live_conversation:call'), dict(provider=provider_key,
        max_seconds=15, reservation_usd=.5), spec, publish=events.append)
    original_handler = turn.feature['handler']
    async def diagnose(host):
        try:
            return await original_handler(host)
        except BaseException:
            import traceback
            failures.append(traceback.format_exc())
            raise
    turn.feature = {**turn.feature, 'handler': diagnose}
    task = dict(id='synthetic-live-' + engine, type='task', text='Read the synthetic canary during our call.',
                _is_direct_chat=True, chat_id=spec.chat_id, model=turn.model, use_local_model=False,
                origin_message_ref={'chat_id': spec.chat_id, 'client_message_id': 'synthetic-input'},
                metadata={'execution_transport': turn.request})
    write_task_result(env.drive_root, task['id'], STATUS_RUNNING, chat_id=task['chat_id'])
    _publish(env.drive_root)
    ctx = ToolContext(repo_dir=env.repo_dir, drive_root=env.drive_root, branch_dev='test', task_id=task['id'],
                      current_chat_id=spec.chat_id, is_direct_chat=True, task_metadata=task['metadata'], task_contract={})
    ctx.emit_progress_fn = lambda text, **kw: events.append({'text': text, **kw})
    tools = ToolRegistry(env.repo_dir, env.drive_root)
    tools.set_context(ctx)
    messages, _ = context.build_llm_messages(env, memory, task, ctx=ctx, tool_schemas=tools.schemas(), route_resolver=turn.resolve_route)
    ordinary_messages = json.loads(json.dumps(messages))
    hub, created = open_owner_hub(spec)
    assert created
    hub.join(cp.PeerInfo('owner', cp.PEER_OWNER, True, True), 24000)
    scope = ua.UsageScope(drive_root=env.drive_root, task_id=task['id'], root_task_id=task['id'], global_limit_usd=10)
    def run():
        with ua.usage_scope(scope), task_model_wait_scope(task=task, drive_root=env.drive_root,
                event_queue=None, worker_slot_held=False) as owner:
            ctx.model_wait_context = owner
            owner.tool_context = ctx
            results.append(turn.run(ctx, messages, tools, task=task, initial_effort='', incoming_messages=queue.Queue()))
    thread = threading.Thread(target=run)
    thread.start()
    try:
        wait_for(lambda: hub._state['state'] == cp.STATE_LIVE or results)
        assert not results, results
        assert selected_inputs[0]['mode'] == 'nano'
        selected_messages = json.loads(selected_inputs[0]['messages_json'])
        assert selected_messages == messages
        assert selected_messages[:len(ordinary_messages)] == ordinary_messages
        assert hub.receive_audio('owner', cp.AudioFrame(cp.FRAME_AUDIO_IN, hub.epoch, 0, 24000, 0, b'\x00\x00' * 960))
        wait_for(wire.result_received.is_set)
        # The actual ordinary tool delivery, not a synthetic replacement result.
        output = json.dumps(wire.sent, ensure_ascii=False)
        assert 'SYNTHETIC-CALL-CANARY-ONLY' in output, output[-1000:]
        wait_for(lambda: any(e.get('type') == 'response.done' or e.get('serverContent', {}).get('turnComplete') for e in wire.received))
        if ending == 'bye':
            hub.receive_control('owner', {'type': 'bye', 'reason': 'left'})
        else:
            from supervisor.task_reaper import request_finalization_grace
            from supervisor.owner_stop import REASON_OWNER_STOPPED_DIRECT_TURN
            from ouroboros import extension_loader as loader
            from ouroboros.extension_import_staging import _module_key
            from ouroboros.extension_registry_state import _retiring_imports
            wire.allow_close.clear()
            late = []
            wire.after_close = lambda: late.extend(importlib.import_module(_module_key(skill) + '.late_identity').VALUE
                                                  for skill in (name, 'live_conversation'))
            assert request_finalization_grace(env.drive_root, task['id'], REASON_OWNER_STOPPED_DIRECT_TURN,
                chat_id=spec.chat_id, stamp=0, quiet=True)
            assert wire.closing.wait(5)
            loader.unload_extension(name)
            loader.unload_extension('live_conversation')
            assert {name, 'live_conversation'} <= _retiring_imports.keys()
            assert thread.is_alive() and not wire.closed
            assert not any(e.get('type') == 'conversation_finished' for e in events)
            wire.allow_close.set()
        thread.join(10)
        assert not thread.is_alive() and wire.closed
        if ending == 'bye':
            assert results[0][1]['execution_status'] == 'ok', results
        else:
            assert results[0][1]['reason_code'] in {'finalize_requested', 'cancelled'}, results
            assert late == ['synthetic-old-import'] * 2
            assert name not in _retiring_imports and 'live_conversation' not in _retiring_imports
        assert any(row.get('tool_call_id') == 'read-1' for row in messages)
        # Gemini's item names are local; all final owner captions carry a call
        # identity and remain ordinary canonical chat input.
        from ouroboros.dialogue_store import iter_rows
        stored = list(iter_rows(env.drive_root, 'chat'))
        assert any('Read the synthetic canary.' in str(row) and 'live_call' in str(row) for row in stored), stored
        assert any('Read the synthetic canary' in str(row.get('content', '')) for row in messages)
        ledger = ua.read_usage_records(env.drive_root, final_only=True)
        assert len(ledger) == 1 and ledger[0].get('cost_usd') is None, ledger
        # Usage evidence can settle after Stop while its price remains unknown.
        assert ledger[0]['state'] == 'settled', ledger
        assert not ledger[0].get('cost_final'), ledger
        assert ledger[0].get('prompt_tokens') is None and ledger[0].get('completion_tokens') is None, ledger
        projection = ua.usage_projection(env.drive_root)
        assert projection['unresolved_upper_bound_usd'] == .5, projection
        assert projection['accounted_usd'] == .5 and not projection['cost_final'], projection
        if ending == 'bye':
            assert ledger[0]['cost_evidence']['responses'], ledger
    finally:
        close_hub(spec.call_id, 'fixture_cleanup')
        wire.allow_close.set()
        turn.deadline = 0
        thread.join(10)
        evidence = os.environ.get('OUROBOROS_TEST_EVIDENCE_ROOT')
        if evidence:
            target = Path(evidence) / 'live-consumers' / engine / ending
            target.mkdir(parents=True, exist_ok=True)
            (target / 'raw.json').write_text(json.dumps({'fixture': 'synthetic-only', 'selected_inputs': selected_inputs,
                'ordinary_messages': ordinary_messages, 'sent': wire.sent,
                'received': wire.received, 'events': events, 'results': results, 'alive': thread.is_alive(),
                'closed': wire.closed, 'failures': failures, 'ledger': ua.read_usage_records(env.drive_root),
                'usage_projection': ua.usage_projection(env.drive_root)}, default=str, indent=2))
            artifacts = env.drive_root / 'task_results/artifacts' / task['id']
            if artifacts.exists():
                shutil.copytree(artifacts, target / 'artifacts', dirs_exist_ok=True)
            observed = env.drive_root / 'observability'
            if observed.exists():
                shutil.copytree(observed, target / 'observability', dirs_exist_ok=True)
                (target / 'receipt-path-map.json').write_text(json.dumps({
                    'original_root': str(observed), 'retained_relative_root': 'observability',
                    'note': 'Receipts are unchanged; absolute refs point to the disposable run.'
                }, indent=2))


def test_grok_cumulative_caption_and_outer_meter(packages):
    from ouroboros.extension_import_staging import _module_key
    from ouroboros.contracts.live_conversation import LiveOptions
    from ouroboros.contracts.realtime import RealtimeInput, canonical_json
    module = importlib.import_module(_module_key('grok_realtime'))
    selected = RealtimeInput('synthetic', canonical_json([{'role': 'system', 'content': 'rules'},
        {'role': 'user', 'content': 'hello'}]), '[]', 100, 'nano', 'core', 'view')
    session = module.GrokSession(selected, LiveOptions(), connect=None, observe=lambda *_: None)
    rows = [session.convert({'type': 'conversation.item.input_audio_transcription.completed', 'item_id': 'a',
        'transcript': text, 'status': status})[0] for text, status in [('wrong word', 'in_progress'),
        ('correct word', 'in_progress'), ('correct word here', 'completed')]]
    assert [row.data['replace'] for row in rows] == [True] * 3
    assert [row.data['final'] for row in rows] == [False, False, True]
    usage = [session.convert({'type': 'response.done', 'usage': {'audio_seconds': value},
        'response': {'id': str(i), 'usage': {}, 'output': []}})[0].data['usage'] for i, value in enumerate([2, 3, 5, 5])]
    assert [row['raw_usage']['audio_seconds'] for row in usage] == [2, 3, 5, 5]
    assert all('prompt_tokens' not in row and row['meter'] == 'connection_cumulative' for row in usage)


def test_grok_images_refuse_before_connect(packages):
    from ouroboros.realtime_setup import loaded_provider
    from ouroboros.contracts.live_conversation import LiveOptions
    from ouroboros.contracts.realtime import RealtimeInput, RealtimeSetupError, canonical_json
    selected = RealtimeInput('synthetic', canonical_json([{'role': 'system', 'content': 'rules'},
        {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,eA=='}}]}]),
        '[]', 100, 'nano', 'core', 'view')
    with pytest.raises(RealtimeSetupError, match='vision_unsupported'):
        loaded_provider('grok_realtime:grok')['session'](selected, LiveOptions(), observe=lambda *_: None)
