"""R7 corrections through real consumers; all authority, memory and money are synthetic."""
from __future__ import annotations

import asyncio
import importlib
import json
import shutil
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_realtime_turn import consumer, prepared, wait_for  # noqa: F401
from tests.test_realtime_setup import (  # noqa: F401
    ROOT, PROVIDER, Wire, loaded, _clear_loader_state, selected,
)


def test_obligatory_task_input_pointer_refuses_setup_consumer(consumer):
    from ouroboros import context
    from ouroboros.memory import Memory

    c = consumer
    c.task['text'] = 'EXACT SYNTHETIC OWNER INPUT\n' * 30000
    messages, _ = context.build_llm_messages(
        c.env, Memory(c.env.drive_root), c.task, ctx=c.ctx,
        tool_schemas=c.tools.schemas(), route_resolver=c.turn.resolve_route)
    assert c.ctx.context_fit_plan.nano_projection.user_content_json is not None
    c.messages[:] = messages
    c.thread.start()
    event = wait_for(lambda: next((e for e in c.events if e['type'] in {
        'realtime_prepared', 'realtime_finished'}), None))
    if event['type'] == 'realtime_prepared':  # original consumer actually sends the unread pointer
        c.turn.authorize(event['wire_sha256'], .5)
    c.thread.join(10)
    assert c.result[0][1]['reason_code'] == 'task_input_reader_required', c.result
    assert not c.wire.sent and c.ua.read_usage_records(c.ctx.drive_root) == []


def test_optional_pattern_link_remains_usable(consumer):
    c = consumer
    assert c.ctx.context_fit_plan.nano_projection.user_content_json is None
    c.messages.append({'role': 'system', 'content':
        'Patterns are optional on demand: read_file("memory/registry.md").'})
    preview = prepared(c)
    c.turn.authorize(preview['wire_sha256'], .5)
    c.thread.join(10)
    assert c.result[0][1]['execution_status'] == 'ok', c.result


@pytest.mark.parametrize('scenario', ['overflow', 'bounded64', 'unknown'])
def test_physical_probe_fit_and_bounded_witness(consumer, monkeypatch, scenario):
    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.capability_evidence import MAIN_DENSITY_BASIS
    from ouroboros.context_fit import estimate_context_prompt_tokens
    from ouroboros.contracts.realtime import RealtimeProbe
    from ouroboros.tool_policy import initial_tool_schemas
    from ouroboros import capability_evidence

    c = consumer
    c.turn.probe = RealtimeProbe('Return the synthetic room fact.', 64)
    # Actual late input, absent from the plan's cached projection estimates.
    c.messages.append({'role': 'user', 'content': 'LATE EXACT PAYLOAD ' * 25000})
    raw = estimate_context_prompt_tokens(
        [*c.messages, {'role': 'user', 'content': c.turn.probe.text}],
        initial_tool_schemas(c.tools, context_mode='nano'), provider=c.ctx.context_fit_plan.provider)
    # This large synthetic payload needs matching usage, not Wire's tiny default.
    def payload_usage(reply, _event):
        if reply['type'] == 'response.done':
            reply['response']['usage']['input_tokens'] = raw
        return reply

    c.wire.alter = payload_usage
    window = raw + (63 if scenario == 'overflow' else 64)
    c.ctx.context_fit_plan = replace(c.ctx.context_fit_plan, window_tokens=window,
                                    status='unknown' if scenario == 'unknown' else c.ctx.context_fit_plan.status)
    requests, witnesses = [], []
    original = c.ua.execute_physical_attempt_async
    density = capability_evidence.record_token_density

    async def attempt(request, *args, **kwargs):
        requests.append(request)
        return await original(request, *args, **kwargs)

    def record(*args, **kwargs):
        witnesses.append(kwargs)
        return density(*args, **kwargs)

    monkeypatch.setattr(c.ua, 'execute_physical_attempt_async', attempt)
    monkeypatch.setattr(capability_evidence, 'record_token_density', record)
    monkeypatch.setattr(capability_evidence, '_DENSITY_MEMO', {})
    preview = prepared(c)
    capture = json.loads(read_actor_source_bytes(c.ctx.drive_root, c.ctx.task_id, preview['source_ref']))
    assert capture['fit']['measurement']['response_reserve_tokens'] == 8192
    assert capture['fit']['predicted_capacity_miss'] is (scenario != 'unknown')
    assert c.wire.sent == [] and c.ua.read_usage_records(c.ctx.drive_root) == []
    c.turn.authorize(preview['wire_sha256'], .5)
    c.thread.join(10)
    assert len(requests) == 1
    request = requests[0]
    assert request.prompt_tokens_bounded_estimate == raw, request
    physical = capture['physical_fit']['measurement']
    assert physical['raw_input_tokens'] == raw
    assert physical['response_reserve_tokens'] == request.max_completion_tokens == 64
    assert physical['capacity_total_tokens'] == (None if scenario == 'unknown' else window)
    rows = c.ua.read_usage_records(c.ctx.drive_root, final_only=True)
    if scenario == 'overflow':
        assert c.result[0][1]['reason_code'] == 'context_capacity', c.result
        assert not c.wire.sent
        assert len(rows) == 1 and rows[0]['state'] == 'released', rows
        assert not witnesses
    else:
        assert c.result[0][1]['execution_status'] == 'ok', c.result
        assert witnesses and witnesses[-1]['basis'] == MAIN_DENSITY_BASIS, witnesses
        assert witnesses[-1]['prompt_chars'] == raw * 4
        assert witnesses[-1]['route_fp'] == c.ctx.context_fit_plan.route_fp
        pairs = [pair for item in capability_evidence._load(c.ctx.drive_root)['token_density'].values()
                 for pair in item['pairs']]
        assert any(pair['basis'] == MAIN_DENSITY_BASIS and pair['prompt_chars'] == raw * 4
                   and pair['route_fp'] == c.ctx.context_fit_plan.route_fp for pair in pairs), pairs
        assert json.loads(capture['selected']['messages_json']) == c.messages
        assert c.wire.sent[-1]['response']['max_output_tokens'] == 64
        assert len(rows) == 1 and rows[0]['state'] == 'settled'


@pytest.fixture
def local_provider(tmp_path):
    from ouroboros import extension_loader as loader
    from ouroboros.skill_loader import find_skill, save_enabled, SkillReviewState

    drive = tmp_path / 'local-drive'
    directory = drive / 'skills/external/openai_realtime'
    shutil.copytree(ROOT / 'skills/openai_realtime', directory)
    (directory / 'late_identity.py').write_text('VALUE = "synthetic-active-import"\n')
    skill = find_skill(drive, 'openai_realtime')
    save_enabled(drive, skill.name, True)
    skill = replace(skill, enabled=True, manifest=replace(
        skill.manifest, env_from_settings=[], permissions=['realtime_provider']),
        review=SkillReviewState(status='pass', content_hash=skill.content_hash))
    assert loader.load_extension(skill, lambda: {}, drive_root=drive) is None
    yield skill, drive
    loader.unload_extension(skill.name)


@pytest.mark.parametrize('reinstall', [False, True])
def test_local_delete_keeps_imports_until_actual_teardown(local_provider, reinstall):
    from ouroboros.extension_import_staging import _module_key
    from ouroboros.extension_registry_state import _extensions, _retiring_imports
    from ouroboros.realtime_setup import run_realtime_setup
    from ouroboros.skill_uninstall_state import delete_local_skill

    skill, drive = local_provider
    import_root = Path(_extensions[skill.name].import_root)
    state_dir = drive / 'state/skills' / skill.name
    late = []

    async def scenario():
        closing, finish = asyncio.Event(), asyncio.Event()
        wire = Wire(lambda *_: None)

        @asynccontextmanager
        async def connect(model):
            try:
                yield wire
            finally:
                closing.set()
                await finish.wait()
                try:
                    late.append(importlib.import_module(_module_key(skill.name) + '.late_identity').VALUE)
                except Exception as exc:
                    late.append(type(exc).__name__)
                wire.closed = True

        preview = await run_realtime_setup(PROVIDER, selected(), timeout_s=10, observe=lambda *_: None)
        operation = asyncio.create_task(run_realtime_setup(
            PROVIDER, selected(), timeout_s=10, observe=lambda *_: None, execute=True,
            expected_wire_sha256=preview.wire_sha256, connect=connect))
        while not wire.sent:
            await asyncio.sleep(0)
        result = delete_local_skill(drive, skill)
        await closing.wait()
        retained = import_root.exists()
        pending = skill.name in _retiring_imports and not operation.done()
        if reinstall:
            Path(skill.skill_dir).mkdir()
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / 'new-owner-state.json').write_text('{"synthetic_reinstall":true}')
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert retained and pending, (retained, pending, result, late)
        assert result['ok'] and result['state_delete_pending'] and not result['deleted_state'], result
        assert late == ['synthetic-active-import'], late
        assert wire.closed
        if reinstall:
            assert (state_dir / 'new-owner-state.json').read_text() == '{"synthetic_reinstall":true}'
        else:
            assert not state_dir.exists() and not Path(skill.skill_dir).exists()
        assert skill.name not in _retiring_imports
        assert _module_key(skill.name) not in sys.modules
    asyncio.run(scenario())


def test_idle_local_delete_still_removes_state_immediately(local_provider):
    from ouroboros.skill_uninstall_state import delete_local_skill

    skill, drive = local_provider
    result = delete_local_skill(drive, skill)
    assert result['ok'] and result['deleted_state'] and not result.get('state_delete_pending'), result
    assert not (drive / 'state/skills' / skill.name).exists()
    assert not Path(skill.skill_dir).exists()
