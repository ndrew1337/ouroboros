"""OpenAI wire grammar and host output evidence through isolated real consumers.

Run with unchanged scripts/safe_test.py. Synthetic wires/ledger only; no provider
or native qualification. PR2076's shared pre-send refusal is a separate join.
"""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import asdict, replace

import pytest

from ouroboros.contracts.live_conversation import LiveOptions
from ouroboros.contracts.realtime import RealtimeProbe, RealtimeSetupError
from ouroboros.realtime_setup import loaded_provider
from tests._extension_loader_shared import _clear_loader_state  # noqa: F401
from tests.test_live_conversation import VoiceWire, packages  # noqa: F401
from tests.test_live_provider_boundaries import selected
from tests.test_live_provider_regressions import deny_network, evidence, ordinary_host  # noqa: F401


class OutputWire(VoiceWire):
    """A full-session echo, with the documented output field grammar enforced."""

    def __init__(self, changed_at=None):
        super().__init__("openai", None)
        self.changed_at = changed_at
        self.acks = 0

    async def send(self, raw):
        event = json.loads(raw)
        value = (event.get("session") or event.get("response") or {}).get("max_output_tokens")
        if value is not None:
            assert value == "inf" or type(value) is int and 1 <= value <= 4096
        await super().send(raw)

    def put(self, event):
        if event.get("type") == "session.updated":
            self.acks += 1
            if self.acks == self.changed_at:
                event = copy.deepcopy(event)
                event["session"]["max_output_tokens"] = 4096
        super().put(event)


@pytest.mark.parametrize("allowance", [1, 256, 4096, 4097, 8192, 50000])
def test_openai_wire_field_preserves_the_ordinary_allowance(packages, evidence, allowance):
    value = replace(selected(), output_allowance_tokens=allowance)
    original = asdict(value)
    wire = OutputWire()
    adapter = loaded_provider("openai_realtime:openai")["session"](
        value, LiveOptions(), connect=wire.connect, observe=lambda *_: None)

    async def run():
        try:
            await adapter.open()
            assert (await adapter.receive())[0].kind == "ready"
        finally:
            await adapter.close()
            evidence.update(sent=wire.sent, received=wire.received, selected=asdict(value),
                            output_bound_tokens=adapter.output_bound_tokens)
    asyncio.run(run())
    assert asdict(value) == original and adapter.selected is value
    expected = allowance if allowance <= 4096 else "inf"
    assert adapter.wire["events"][0]["session"]["max_output_tokens"] == expected
    assert wire.sent[0]["session"]["max_output_tokens"] == expected
    assert adapter.output_bound_tokens == (allowance if allowance <= 4096 else None)
    assert wire.closed


@pytest.mark.parametrize("changed_at", [1, 2, 3])
def test_openai_output_field_ack_must_match_at_setup_vad_and_later_updates(packages, evidence, changed_at):
    wire = OutputWire(changed_at)
    adapter = loaded_provider("openai_realtime:openai")["session"](
        replace(selected(), output_allowance_tokens=8192), LiveOptions(model_output_bound_tokens=50000),
        connect=wire.connect, observe=lambda *_: None)

    async def run():
        try:
            with pytest.raises(RealtimeSetupError, match="session_mismatch.*max_output_tokens"):
                await adapter.open()
                await adapter.receive()  # ready
                await adapter.write({"type": "session.update", "session": {"type": "realtime"}})
                await adapter.receive()
        finally:
            await adapter.close()
            evidence.update(sent=wire.sent, received=wire.received)
    asyncio.run(run())
    assert wire.closed
    assert not any(row.get("type") == "response.create" for row in wire.sent)


@pytest.mark.parametrize("case,allowance,expected", [
    ("fresh", 8192, 50000),
    ("fresh_smaller", 8192, 6000),
    ("owner_ack", 8192, 50000),
    ("missing", 8192, None),
    ("stale", 8192, None),
    ("other_model", 8192, None),
    ("other_endpoint", 8192, None),
    ("other_provider", 8192, None),
    ("mismatched_record", 8192, None),
    ("undated", 8192, None),
    ("zero", 8192, None),
    ("missing", 256, 256),
    ("fresh", 256, 256),
])
def test_openai_host_resolves_actual_bound_and_seals_physical_facts(
        ordinary_host, monkeypatch, evidence, case, allowance, expected):
    from ouroboros import capability_evidence as ce, response_limits as limits, usage_accounting as ua
    from ouroboros.observability import read_call_manifest_ref

    host = ordinary_host.host
    root = host.ctx.drive_root
    route = dict(provider=host.turn.route.accounting_provider, model=host.turn.model,
                 base_url=host.turn.route.endpoint)
    # A synthetic known window, independent of the response record's date.
    host.plan = replace(host.plan, output_reserve_tokens=allowance,
                        status="confirmed", stale=False, window_tokens=1000000)
    host.ctx.context_fit_plan = host.plan
    value, fit = host.selected_input()
    assert value.output_allowance_tokens == allowance
    original = asdict(value)
    model_bound = None
    if case != "missing":
        observed_route = dict(route)
        if case == "other_model":
            observed_route["model"] += "-other"
        elif case == "other_endpoint":
            observed_route["base_url"] = "wss://other.invalid/realtime"
        elif case == "other_provider":
            observed_route["provider"] = "synthetic-other"
        maximum = 6000 if case == "fresh_smaller" else 0 if case == "zero" else 50000
        if case == "owner_ack":
            limits.record_response_ack(root, **route, max_output_tokens=maximum)
        else:
            record = limits.record_metadata_limit(root, **observed_route, maximum=maximum,
                source="synthetic-output-bound-fixture",
                **({"observed_at": "2000-01-01T00:00:00Z"} if case == "stale" else {}))
            if case in {"mismatched_record", "undated"}:
                changed = {**asdict(record), **({"route_fp": "different-route"} if case == "mismatched_record"
                                               else {"observed_at": ""})}
                ce._store_evidence(root, "response_limits", record.route_fp, changed)
        if case in {"fresh", "fresh_smaller", "owner_ack"}:
            model_bound = maximum

    resolutions, requests, sessions = [], [], []
    resolve = limits.resolve_response_limit

    def resolved(drive_root, **kwargs):
        assert drive_root == root
        assert kwargs == {**route, "allow_fetch": False}
        result = resolve(drive_root, **kwargs)
        resolutions.append(asdict(result))
        return result

    monkeypatch.setattr(limits, "resolve_response_limit", resolved)
    provider = loaded_provider(host.turn.provider)
    wire = OutputWire()
    factory = provider["session"]

    def session(selected_input, options, *, observe):
        assert asdict(selected_input) == original
        assert options.model_output_bound_tokens == model_bound
        adapter = factory(selected_input, options, connect=wire.connect, observe=observe)
        sessions.append(adapter)
        return adapter

    from ouroboros.extension_registry_state import _realtime_providers
    monkeypatch.setitem(_realtime_providers[host.turn.provider], "session", session)
    execute = ua.execute_physical_attempt_async

    async def capture(request, *args, **kwargs):
        requests.append(request)
        return await execute(request, *args, **kwargs)

    monkeypatch.setattr(ua, "execute_physical_attempt_async", capture)

    async def consume(adapter, reported):
        assert (await adapter.receive())[0].kind == "ready"
        assert adapter.output_bound_tokens == expected
        return "synthetic-complete"

    try:
        with ua.usage_scope(ua.UsageScope(drive_root=root, task_id=host.ctx.task_id,
                                        root_task_id=host.ctx.task_id, global_limit_usd=10)):
            assert asyncio.run(host.session(consume)) == "synthetic-complete"
        assert len(requests) == len(resolutions) == len(sessions) == 1
        request = requests[0]
        reserve = expected if expected is not None else allowance
        assert request.max_completion_tokens == reserve
        assert request.prompt_tokens_estimate == fit.measurement.estimated_input_tokens
        assert request.prompt_tokens_bounded_estimate == fit.measurement.raw_input_tokens
        assert request.physical_context.capacity_total_tokens == fit.measurement.capacity_total_tokens == 1000000
        assert request.physical_context.profile == fit.measurement.profile == "owner_nano"
        assert request.physical_context.rendered_mode == "nano"
        assert request.max_budget_usd == .5 and request.force_unknown_reservation
        assert request.allow_live_fetch is False
        assert host.selected_input()[0] == value  # wire encoding never narrows ordinary selection
        rows = ua.read_usage_records(root, final_only=True)
        assert len(rows) == 1 and rows[0]["reservation_upper_bound_usd"] == .5
        receipt = read_call_manifest_ref(root, rows[0]["candidate_manifest_ref"], task_id=host.ctx.task_id)
        assert receipt["native_output_bound_tokens"] == expected
        assert receipt["selected_output_allowance_tokens"] == allowance
        assert receipt["output_reserve_tokens"] == reserve
        assert receipt["output_reserve_kind"] == ("native_token_bound" if expected is not None else "host_estimate_only")
        assert receipt["response_limit"] == resolutions[0]
        assert receipt["selected_input_fit"] == asdict(fit)
        assert receipt["physical_context"] == asdict(request.physical_context)
        assert receipt["candidate_context_sha256"] == value.sha256
        assert receipt["model_send_seal"]["pre_redaction_sha256"] == request.candidate_raw_sha256
        assert wire.sent[0]["session"]["max_output_tokens"] == (allowance if allowance <= 4096 else "inf")
        assert wire.closed
        evidence.update(receipt=receipt, ledger=rows)
    finally:
        evidence.update(requests=[asdict(r) for r in requests], response_limits=resolutions,
                        selected_allowance=allowance, expected_bound=expected,
                        sent=wire.sent, received=wire.received,
                        raw_host_wire=host.wire_path.read_text() if host.wire_path.exists() else "")


@pytest.mark.parametrize("maximum", [None, 50000])
def test_openai_ordinary_prediction_reaches_physical_request_without_refit_or_refusal(
        ordinary_host, monkeypatch, evidence, maximum):
    from ouroboros import usage_accounting as ua
    from ouroboros.response_limits import record_metadata_limit

    host = ordinary_host.host
    host.plan = replace(host.plan, output_reserve_tokens=8192)
    _, measured = host.selected_input()
    host.plan = replace(host.plan, status="confirmed", stale=False,
                        window_tokens=measured.measurement.estimated_input_tokens + 8191)
    host.ctx.context_fit_plan = host.plan
    plan = host.plan
    messages = copy.deepcopy(host.messages)
    value, fit = host.selected_input()
    assert fit.predicted_capacity_miss and value.output_allowance_tokens == 8192
    if maximum is not None:
        record_metadata_limit(host.ctx.drive_root, provider=host.turn.route.accounting_provider,
            model=host.turn.model, base_url=host.turn.route.endpoint, maximum=maximum,
            source="synthetic-output-bound-fixture")

    def no_refit(*args, **kwargs):
        raise AssertionError("An ordinary prediction cannot rewrite selected input before native output is known")

    monkeypatch.setattr(type(plan), "reproject_for_route", no_refit)
    from ouroboros.extension_registry_state import _realtime_providers
    factory = loaded_provider(host.turn.provider)["session"]
    wire = OutputWire()
    sessions, requests = [], []

    def session(selected_input, options, *, observe):
        assert selected_input == value
        adapter = factory(selected_input, options, connect=wire.connect, observe=observe)
        sessions.append(adapter)
        return adapter

    monkeypatch.setitem(_realtime_providers[host.turn.provider], "session", session)

    class PhysicalRequestReached(Exception):
        pass

    async def intercept(request, *args, **kwargs):
        # Stop before accounting/dispatch: this test proves data reaches the
        # shared fit seam, not that an oversized native request is admitted.
        requests.append(request)
        raise PhysicalRequestReached

    monkeypatch.setattr(ua, "execute_physical_attempt_async", intercept)
    with pytest.raises(PhysicalRequestReached):
        asyncio.run(host.session(None))
    assert len(requests) == len(sessions) == 1
    assert sessions[0].output_bound_tokens == maximum
    assert sessions[0].wire["events"][0]["session"]["max_output_tokens"] == "inf"
    request = requests[0]
    assert request.max_completion_tokens == (maximum if maximum is not None else 8192)
    assert request.physical_context.capacity_total_tokens == plan.window_tokens
    assert request.prompt_tokens_estimate == fit.measurement.estimated_input_tokens
    assert host.plan is plan and host.ctx.context_fit_plan is plan
    assert host.messages == messages and host.selected_input()[0] == value
    assert not wire.sent and not ua.read_usage_records(host.ctx.drive_root)
    evidence.update(selected_input_fit=asdict(fit), request=asdict(request),
                    native_output_bound_tokens=maximum, sent=wire.sent)


def test_openai_diagnostic_keeps_independent_numeric_bound(packages, evidence):
    from ouroboros.realtime_setup import run_realtime_setup

    value = replace(selected(), output_allowance_tokens=8192)
    result = asyncio.run(run_realtime_setup("openai_realtime:openai", value, timeout_s=5,
        observe=lambda *_: None, probe=RealtimeProbe("Return the synthetic canary.", 64)))
    events = json.loads(result.wire_json)["events"]
    evidence.update(wire=json.loads(result.wire_json), selected=asdict(value))
    assert events[0]["session"]["max_output_tokens"] == "inf"
    assert events[-1]["response"]["max_output_tokens"] == 64
    assert value.output_allowance_tokens == 8192
