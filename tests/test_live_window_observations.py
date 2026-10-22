"""Live window facts belong to the connection that received the provider input."""
import asyncio
from dataclasses import replace

import pytest

from ouroboros import capability_evidence as ce
from ouroboros.context_window import resolve_window
from ouroboros.contracts.live_conversation import LiveOptions
from tests.test_live_conversation import host_world as host_world
from tests.test_live_usage import token_usage


@pytest.mark.parametrize('window,basis,can_raise,accepted', [
    (133554, 'provider', True, 150000),
    (92000, 'provider', False, 150000),
    (80000, 'estimator', False, 0),
])
def test_completed_input_keeps_units_reserve_and_active_account(
        host_world, monkeypatch, window, basis, can_raise, accepted):
    host = host_world.host
    root = host.ctx.drive_root
    monkeypatch.setattr(ce, 'canonical_evidence_root', lambda: root)
    host.model = replace(host.model, facts=replace(host.model.facts,
        conversation_window_tokens=window, basis=basis, acceptance_can_raise=can_raise))
    old_scope = host.turn.observation_fingerprint()
    factory = host_world.provider['session']

    def large_output_bound(*args, **kwargs):
        session = factory(*args, **kwargs)
        session.output_bound_tokens = 18000
        return session

    host_world.provider['session'] = large_output_bound

    async def consume(session):
        # A settings edit during the connection cannot reattribute its receipt.
        host_world.settings['TEST_CREDENTIAL'] = 'rotated-synthetic-account'
        host.response_started('accepted')
        for _ in range(2):  # Duplicate provider completions cannot create another fact.
            host.response_done('accepted', usage=token_usage(text_input_tokens=100000,
                audio_input_tokens=50000, cached_input_tokens=80000, text_output_tokens=1000),
                raw_usage={'input_tokens': 150000, 'output_tokens': 1000,
                           'input_token_details': {'cached_tokens': 80000}}, status='completed')
        current = resolve_window(host.plan, 18000, root=root)
        assert current.route_fp == old_scope and current.accepted == accepted
        assert current.reserve == 18000
        assert current.total_equivalent == (168000 if can_raise else window)
        assert current.usable == (150000 if can_raise else window - 18000)
        rows = ce._load(root).get('main_windows', {}).get(old_scope, {}).get('observations', [])
        assert len(rows) == (1 if accepted else 0)
        if rows:
            assert rows[0]['accepted'] == 150000 and rows[0]['reserve'] == 18000
        host._held_input, host._growth = 140000, 0
        host._history_at_receipt = host._history_estimate()
        with monkeypatch.context() as polling:
            polling.setattr(ce, '_load', lambda *a, **kw: pytest.fail('audio polling read the window store'))
            for _ in range(1000):
                assert host.renewal_due() is (not can_raise)

    async def rotated(session):
        assert session.opened
        assert host.plan.window_record.route_fp != old_scope
        assert host.plan.window_record.accepted == 0
        assert host._renewal_window.total_equivalent == window

    async def run():
        await host.session(consume, LiveOptions())
        await host.session(rotated, LiveOptions())

    asyncio.run(run())


@pytest.mark.parametrize('status,raw', [
    ('failed', {'input_tokens': 200000}),
    ('cancelled', {'input_tokens': 200000}),
    ('interrupted', {'input_tokens': 200000}),
    ('completed', {}),
    ('completed', {'input_tokens': True}),
    ('completed', {'output_tokens': 200000}),
])
def test_only_completed_provider_input_is_window_evidence(host_world, monkeypatch, status, raw):
    host = host_world.host
    monkeypatch.setattr(ce, 'canonical_evidence_root', lambda: host.ctx.drive_root)
    host.model = replace(host.model, facts=replace(host.model.facts,
        conversation_window_tokens=133554, acceptance_can_raise=True))

    async def consume(session):
        host.response_started('unwitnessed')
        host.response_done('unwitnessed', usage=token_usage(text_input_tokens=200000),
                           raw_usage=raw, status=status)
        assert resolve_window(host.plan, 10000, root=host.ctx.drive_root).accepted == 0
        assert host._renewal_window.total_equivalent == 133554

    asyncio.run(host.session(consume, LiveOptions()))
