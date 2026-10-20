"""Route identity and physical admission for the ordinary call host."""
import time
from dataclasses import replace


def wait_for(predicate, timeout=10):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError("timed out waiting for consumer evidence")


def test_route_reserve_survives_nano_floor():
    from ouroboros.context_budget import context_mode_limits
    from ouroboros.llm_attempt import main_context_fit_refusal
    from ouroboros.usage_accounting import AttemptRequest, PhysicalAttemptContext
    physical = PhysicalAttemptContext("owner_nano", "nano", "cold_estimate", "route", "round",
        None, 90000, False, False, 1.0)
    request = AttemptRequest("model", "fixture_live", max_completion_tokens=32000,
        prompt_tokens_bounded_estimate=80001, physical_context=physical, working_reserve_tokens=10000)
    assert main_context_fit_refusal(request)["free_tokens"] == -1
    assert main_context_fit_refusal(replace(request, prompt_tokens_bounded_estimate=79999)) is None
    assert main_context_fit_refusal(replace(request, working_reserve_tokens=None)) is None
    assert context_mode_limits("nano", "nano", 32000, working_reserve_tokens=20000)[1] == 20000


def test_route_facts_store_acceptance_without_raising_window(tmp_path):
    from ouroboros.capability_evidence import record_route_facts, probe
    from ouroboros.contracts.realtime import RouteFacts
    route = dict(provider="fixture_live", model="fixture::voice", base_url="wss://example.invalid/live")
    facts = RouteFacts(90000, 231828, 10000, "o200k", "fixture retention probe", "2026-10-21")
    record_route_facts(tmp_path, **route, facts=facts)
    observed = probe(tmp_path, **route, allow_fetch=False)
    assert observed.window_tokens == 90000
    assert observed.route_facts["accepted_input_tokens"] == 231828
    assert observed.route_facts["basis"] == "o200k"
    record_route_facts(tmp_path, **route, facts=RouteFacts())
    assert probe(tmp_path, **route, allow_fetch=False).window_tokens == 0


def test_metadata_does_not_recreate_process_local_call_authority():
    from ouroboros.realtime_turn import recovery_refusal
    task = {"metadata": {"execution_transport": {"kind": "live_conversation", "call_id": "call"}}}
    assert recovery_refusal(task, None) == "realtime_restart_required"
    assert recovery_refusal({"metadata": {}}, None) == ""
