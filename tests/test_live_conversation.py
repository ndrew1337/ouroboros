"""Isolated consumers only. No credentials, provider sockets or device capture.

Run through scripts/safe_test.py. Echoes prove our consumers, not vendor acceptance.
Raw wire/ledger receipts are exported even when assertions fail.
"""
from __future__ import annotations
import asyncio
import base64
import json
import shutil
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests._extension_loader_shared import _clear_loader_state  # noqa: F401

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



class FakeSession:
    output_bound_tokens = 100
    renewal_ready = True

    def __init__(self, selected, options, *, observe):
        self.selected, self.options = selected, options
        self.wire = {'messages': json.loads(selected.messages_json), 'tools': json.loads(selected.tools_json),
                     'instructions': options.instructions}
        self.opened = self.closed = False

    async def open(self):
        self.opened = True

    async def close(self):
        self.closed = True


@pytest.fixture
def host_world(tmp_path, monkeypatch, provider_catalog_offline):
    from ouroboros import config, context, extension_registry_state as registry
    from ouroboros.contracts.live_conversation import LiveConfig, CallTool
    from ouroboros.contracts.realtime import CatalogModel, ProviderCatalog, RouteFacts, RealtimeRoute
    from ouroboros.live_conversation import LiveConversationTurn, ConversationHost
    from ouroboros.call_protocol import CallSpec
    from ouroboros.call_peers import LiveCallHub
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.tools.tool_context import ToolContext
    from ouroboros.task_results import write_task_result, STATUS_RUNNING
    from tests._memory_view_context import world
    from tests.test_budget_fence_liveness import _publish
    from ouroboros.model_wait import task_model_wait_scope
    from supervisor import message_bus

    env, memory, rooms = world(tmp_path / 'world')
    monkeypatch.setattr(config, 'DATA_DIR', env.drive_root)
    monkeypatch.setenv('OUROBOROS_DATA_DIR', str(env.drive_root))
    monkeypatch.setattr(context, 'get_context_mode', lambda: 'nano')
    monkeypatch.setattr(message_bus, 'DATA_DIR', env.drive_root)
    monkeypatch.setattr(message_bus, '_BRIDGE', message_bus.LocalChatBridge())
    settings = {'TEST_CREDENTIAL': 'test-credential'}
    async def language(host, arguments):
        host.state['languages'] = arguments['languages']
        return 'Language selection retained.'
    async def handler(host):
        pass
    call_tool = CallTool('set_test_language', 'Set the test language.',
        {'type': 'object', 'properties': {'languages': {'type': 'array', 'items': {'type': 'string'}}}}, language, silent=True)
    live_config = LiveConfig(enabled=True, provider='fixture:voice')
    feature = dict(skill='fixture', key='fixture:call', extension_generation='generation',
                   content_hash='fixture-content', configure=lambda: live_config, handler=handler, call_tools=(call_tool,))
    catalog = ProviderCatalog('Fixture engine', (CatalogModel('voice-test', 'Voice test',
        RouteFacts(200000, 250000, 10000, 'provider', 'fixture', '2026-10-21'), default=True, vision=True),),
        ('TEST_CREDENTIAL',))
    sessions = []
    def factory(*args, **kw):
        value = FakeSession(*args, **kw)
        sessions.append(value)
        return value
    provider = dict(skill='fixture', extension_generation='generation', content_hash='fixture-content',
        catalog=catalog, route=RealtimeRoute('fixture_live', 'wss://example.invalid/live', 'fixture::'), session=factory)
    bundle = SimpleNamespace(generation_digest='generation', supervised_futures=[],
                             api_instances=[SimpleNamespace(get_settings=lambda keys: dict(settings))])
    monkeypatch.setitem(registry._extensions, 'fixture', bundle)
    monkeypatch.setitem(registry._realtime_providers, 'fixture:voice', provider)
    monkeypatch.setitem(registry._conversation_features, 'fixture:call', feature)
    spec = CallSpec('fixture-call', rooms['alpha'], live_config.provider, '', '', client_message_id='call:fixture-call:open')
    turn = LiveConversationTurn(feature, live_config, spec, publish=lambda event: None)
    task = dict(id='fixture-call-task', type='task', text='The owner opened a call.', _is_direct_chat=True,
        chat_id=spec.chat_id, model=turn.model, use_local_model=False,
        origin_message_ref={'chat_id': spec.chat_id, 'client_message_id': spec.client_message_id},
        metadata={'execution_transport': turn.request})
    write_task_result(env.drive_root, task['id'], STATUS_RUNNING, chat_id=spec.chat_id)
    _publish(env.drive_root)
    ctx = ToolContext(repo_dir=env.repo_dir, drive_root=env.drive_root, branch_dev='test', task_id=task['id'],
        current_chat_id=spec.chat_id, is_direct_chat=True, task_metadata=task['metadata'], task_contract={})
    progress = []
    ctx.emit_progress_fn = lambda text, **kw: progress.append({'text': text, **kw})
    tools = ToolRegistry(env.repo_dir, env.drive_root)
    tools.set_context(ctx)
    messages, _ = context.build_llm_messages(env, memory, task, ctx=ctx, tool_schemas=tools.schemas(), route_resolver=turn.resolve_route)
    hub = LiveCallHub(turn.spec)
    with task_model_wait_scope(task=task, drive_root=env.drive_root, event_queue=None, worker_slot_held=False) as owner:
        ctx.model_wait_context, owner.tool_context = owner, ctx
        host = ConversationHost(turn, ctx, messages, tools, task, '', hub)
        yield SimpleNamespace(host=host, env=env, ctx=ctx, settings=settings, config=live_config, provider=provider,
            sessions=sessions, feature=feature, task=task, progress=progress)
        asyncio.run(host.finish())
        hub.close('fixture_done')
        registry._extensions.pop('fixture', None)
        registry._realtime_providers.pop('fixture:voice', None)
        registry._conversation_features.pop('fixture:call', None)


def test_typed_port_default_model_offer_and_unknown_model(host_world):
    from ouroboros.live_conversation import call_offer, LiveConversationTurn
    from ouroboros.contracts.realtime import RealtimeSetupError
    state, host = host_world, host_world.host
    assert host.config is state.config and host.model.id == 'voice-test'
    assert host.state == {} and host.opening
    assert call_offer(state.config)['offered'] is True
    assert call_offer(replace(state.config, enabled=False))['reason'] == 'calls_disabled'
    assert call_offer(replace(state.config, provider='missing:voice'))['reason'] == 'provider_unavailable'
    state.settings.clear()
    assert call_offer(state.config)['reason'] == 'credential'
    state.settings['TEST_CREDENTIAL'] = 'test-credential'
    with pytest.raises(RealtimeSetupError, match='model_unavailable'):
        LiveConversationTurn(state.feature, replace(state.config, model='unknown'), host.turn.spec, publish=lambda _: None)


def test_every_session_refits_current_canonical_rows_and_options(host_world, monkeypatch):
    from ouroboros.context_fit import ContextFitPlan
    from ouroboros.contracts.live_conversation import LiveOptions
    host = host_world.host
    refits = []
    original = ContextFitPlan.reproject_for_route
    def refit(plan, **kwargs):
        refits.append(json.loads(json.dumps(kwargs['current_messages'])))
        return original(plan, **kwargs)
    monkeypatch.setattr(ContextFitPlan, 'reproject_for_route', refit)
    async def consume(session):
        assert session.opened
    async def run():
        await host.session(consume, LiveOptions(instructions='Feature then owner instructions', languages=('ru-RU',)))
        await host.observe_context({'content': 'NEW PEER FACT'})
        await host.session(consume, LiveOptions(instructions='Updated instructions'))
    asyncio.run(run())
    assert len(refits) == 2
    assert 'NEW PEER FACT' not in canonical(refits[0]) and 'NEW PEER FACT' in canonical(refits[1])
    assert 'Feature then owner instructions' in canonical(refits[0])
    assert all(session.closed for session in host_world.sessions)
    assert host_world.sessions[0].options.languages == ('ru-RU',)
    assert host.messages[-1]['_host_context_kind'] == 'call_observation'


def canonical(value):
    return json.dumps(value, ensure_ascii=False)


def test_physical_dispatch_minus_one_refits_or_refuses_and_positive_passes(host_world, monkeypatch):
    from ouroboros.contracts.live_conversation import LiveOptions
    from ouroboros.contracts.realtime import RealtimeSetupError
    host = host_world.host
    host.model = replace(host.model, facts=replace(host.model.facts, basis='o200k',
        conversation_window_tokens=20000, working_reserve_tokens=10000))
    host.count_input = lambda wire: 10001
    async def consume(session):
        pytest.fail('known overflowing physical request was dispatched')
    with pytest.raises(RealtimeSetupError, match='context_capacity'):
        asyncio.run(host.session(consume, LiveOptions()))
    assert not any(session.opened for session in host_world.sessions)
    host.count_input = lambda wire: 9999
    async def allowed(session):
        assert session.opened
        host.response_started('fits')
        host.response_done('fits', usage={}, raw_usage={}, status='completed')
    asyncio.run(host.session(allowed, LiveOptions()))
    assert host_world.sessions[-1].opened
    from ouroboros.usage_accounting import read_usage_records
    row, = read_usage_records(host.ctx.drive_root)
    assert row['physical_context']['capacity_total_tokens'] == 20000
    assert row['physical_context']['measurement_density'] > 0
    prepared = [json.loads(line) for line in host.wire_path.read_text().splitlines()]
    assert any(json.loads(row['raw']).get('counter') == 'o200k' for row in prepared if row['direction'] == 'host')


def test_call_tool_silent_replay_and_existing_row_change_receipt(host_world):
    host = host_world.host
    original = host.messages[0]
    async def edit(host, args):
        original['content'] = 'Changed existing system row.'
        return 'done'
    host.call_tools['set_test_language'] = replace(host.call_tools['set_test_language'], handler=edit)
    call = {'id': 'local-1', 'type': 'function', 'function': {'name': 'set_test_language', 'arguments': '{}'}}
    async def run():
        rows = await host.tool_batch([call])
        assert rows.silent == ('local-1',)
        assert any(row['content'] == 'Changed existing system row.' for row in rows)
        assert any(row.get('tool_call_id') == 'local-1' for row in rows)
        replay = await host.tool_batch([call])
        assert replay.silent == ('local-1',) and host.rounds == 1
    asyncio.run(run())


def test_tool_collision_refuses_but_distinct_feature_tools_join(host_world):
    from ouroboros.live_conversation import ConversationHost
    from ouroboros.contracts.realtime import RealtimeSetupError
    host = host_world.host
    assert 'set_test_language' in {s['function']['name'] for s in host.schemas}
    colliding = replace(host.call_tools['set_test_language'], name='read_file')
    host.turn.feature = {**host.turn.feature, 'call_tools': (colliding,)}
    with pytest.raises(RealtimeSetupError, match='tool_collision'):
        ConversationHost(host.turn, host.ctx, host.messages, host.tools, host.task, '', host.hub)


def test_captions_are_crash_durable_but_checkpoint_at_completed_turn(host_world, monkeypatch):
    from ouroboros.dialogue_store import iter_rows
    host = host_world.host
    checkpoints = []
    monkeypatch.setattr(host, 'checkpoint', checkpoints.append)
    async def run():
        await host.caption('owner', 'Durable owner speech', 'owner-1')
        await host.caption('owner', 'Durable owner speech', 'owner-1')
        await host.caption('ouroboros', 'Durable reply', 'answer-1', completed=True)
    asyncio.run(run())
    assert not checkpoints
    rows = list(iter_rows(host.ctx.drive_root, 'chat'))
    assert sum('Durable owner speech' in str(row) for row in rows) == 1
    # No start event still leaves a settled, unknown-priced response.
    host.response_done('answer-1', usage={}, raw_usage={}, status='completed')
    assert checkpoints == ['ready']


def test_renewal_uses_reported_held_input_and_growth(host_world):
    host = host_world.host
    host.model = replace(host.model, facts=replace(host.model.facts, conversation_window_tokens=20000, working_reserve_tokens=10000))
    assert host.renewal_due() is False
    host.response_done('one', usage={'text_input_tokens': 4000}, raw_usage={}, status='completed')
    assert host.renewal_due() is False
    host.response_done('two', usage={'text_input_tokens': 8000}, raw_usage={}, status='completed')
    assert host.renewal_due() is True
