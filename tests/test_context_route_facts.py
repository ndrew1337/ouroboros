"""Route units and working room enter the ordinary fitter before memory selection."""
from dataclasses import asdict, replace
import hashlib
import json
import math
from types import SimpleNamespace

import pytest

from ouroboros import context_fit as fit, memory_view
from ouroboros.context_anchor import InputAnchor
from ouroboros.contracts.realtime import RouteFacts
from ouroboros.section_floor import HostSection
from tests import _memory_view_synthetic as syn


@pytest.fixture(autouse=True)
def evidence(monkeypatch):
    monkeypatch.setattr(fit, "main_output_reserve_tokens", lambda **_kw: 16_384)
    monkeypatch.setattr(fit, "_route_calibration_ratio", lambda *_a: 1.0)
    monkeypatch.setattr("ouroboros.capability_evidence.resolve_main_token_density",
                        lambda *_a, **_kw: (1.0, "cold_estimate"))


def core(root, *, memory=False):
    section = HostSection("runtime", "## Runtime\n" + "observed details " * 6000,
                          "## Runtime\nThe operative facts.")
    return fit.ContextCore(base_prompt="SYSTEM", bible_md="BIBLE", architecture_md="# Architecture",
        development_md="# Development", docs_need_development=False, semi_stable_text="IDENTITY",
        dynamic_text=section.full_text, user_content_json=json.dumps("Exact owner words."),
        dynamic_sections=(section,), section_root=str(root), section_task_id="route-facts-test",
        memory_view_json=memory_view.snapshot_json(syn.snapshot(story=syn.pages(3, 4000))) if memory else "")


def plan(captured, window, *, facts=None, mode="nano"):
    evidence = SimpleNamespace(route_fp="test-route", status="confirmed" if window else "unprobeable",
                               stale=False, window_tokens=window, source="test")
    if facts is not None:
        evidence.route_facts = asdict(facts)
    return fit.build_context_fit_plan(SimpleNamespace(drive_root=captured.section_root), captured,
        {"id": "route-facts-test"}, preferred_mode=mode,
        route_resolver=lambda *_a, **_kw: ({"provider": "test", "model": "synthetic"}, evidence))


def addressed(built, mode="nano"):
    return built.projection(mode).memory_facts.get("host_sections_addressed", [])


@pytest.mark.parametrize("mode", ["max", "low", "nano"])
def test_working_reserve_is_independent_of_reply_allowance_at_initial_fit(tmp_path, mode):
    captured = core(tmp_path)
    raw = plan(captured, 0, mode=mode).projection(mode).estimated_tokens
    window = raw + 24_000 - 1
    ordinary = plan(captured, window, mode=mode)
    route = plan(captured, window, mode=mode, facts=RouteFacts(
        conversation_window_tokens=window, accepted_input_tokens=window * 2, working_reserve_tokens=24_000))
    assert not addressed(ordinary, mode)
    assert addressed(route, mode)
    assert route.working_reserve_tokens == 24_000
    assert route.output_reserve_tokens == ordinary.output_reserve_tokens == 16_384
    assert route.window_tokens == window


def test_working_reserve_reaches_the_memory_floor_without_host_sections(tmp_path):
    snapshot = syn.snapshot(story=syn.pages(3, 40_000))
    captured = replace(core(tmp_path), dynamic_text="", dynamic_sections=(),
                       memory_view_json=memory_view.snapshot_json(snapshot))
    ordinary = plan(captured, 70_000)
    route = plan(captured, 70_000, facts=RouteFacts(
        conversation_window_tokens=70_000, working_reserve_tokens=24_000))
    assert ordinary.nano_projection.memory_facts["floor"]["steps"] == {}
    assert route.nano_projection.memory_facts["floor"]["steps"]
    assert route.nano_projection.estimated_tokens < ordinary.nano_projection.estimated_tokens


@pytest.mark.parametrize("basis", ["provider", "estimator"])
def test_unknown_conversation_window_keeps_host_sources_whole(tmp_path, monkeypatch, basis):
    monkeypatch.setattr(fit, "_route_calibration_ratio", lambda *_a: 4.0)
    built = plan(core(tmp_path), 0, facts=RouteFacts(accepted_input_tokens=100_000,
                                                  working_reserve_tokens=24_000, basis=basis))
    assert not addressed(built)
    assert not (tmp_path / "task_results" / "artifacts").exists()
    measure = fit.measure_main_fit(built, built.messages_for("nano"), [], profile="owner_nano",
        rendered_mode="nano", round_id="r1", accepted_input_tokens=100_000).measurement
    assert measure.capacity_total_tokens is None
    assert measure.response_reserve_tokens == 24_000


@pytest.mark.parametrize("basis", ["provider", "estimator"])
@pytest.mark.parametrize("density,cutoff", [(0.25, 0.5), (4.0, 2.0)])
def test_window_basis_applies_before_the_first_memory_selection(tmp_path, monkeypatch, basis, density, cutoff):
    captured = core(tmp_path)
    raw = plan(captured, 0).nano_projection.estimated_tokens
    window = math.ceil(raw * cutoff) + 8192
    monkeypatch.setattr(fit, "main_output_reserve_tokens", lambda **_kw: 8192)
    monkeypatch.setattr(fit, "_route_calibration_ratio", lambda *_a: density)
    projections, calls = fit._view_projections, []

    def record(*args, **kwargs):
        calls.append(kwargs["ratio"])
        return projections(*args, **kwargs)

    monkeypatch.setattr(fit, "_view_projections", record)
    built = plan(captured, window, facts=RouteFacts(conversation_window_tokens=window, basis=basis))
    expected_ratio = density if basis == "provider" else 1.0
    assert calls == [expected_ratio]
    assert built.nano_projection.calibration_ratio == expected_ratio
    assert bool(addressed(built)) == (expected_ratio > cutoff)
    if expected_ratio <= cutoff:
        assert not (tmp_path / "task_results" / "artifacts").exists()


@pytest.mark.parametrize("basis,density", [("provider", 0.5), ("provider", 2.0), ("estimator", 4.0)])
def test_route_reprojection_keeps_working_room_and_declared_units(tmp_path, basis, density):
    captured = core(tmp_path)
    built = plan(captured, 2_000_000, facts=RouteFacts(
        conversation_window_tokens=2_000_000, working_reserve_tokens=24_000, basis=basis))
    raw = built.nano_projection.estimated_tokens
    expected_ratio = density if basis == "provider" else 1.0
    window = math.ceil(raw * expected_ratio) + 24_000
    same = built.reproject_for_route(window_tokens=window, known_window=True, ratio=density,
        output_reserve=16_384, tool_schemas=[], current_messages=built.messages_for("nano"))
    assert same.messages_for("nano") == built.messages_for("nano")
    assert same.nano_projection.calibration_ratio == expected_ratio
    smaller = built.reproject_for_route(window_tokens=window - 1, known_window=True, ratio=density,
        output_reserve=16_384, tool_schemas=[], current_messages=built.messages_for("nano"))
    assert addressed(smaller)
    assert smaller.working_reserve_tokens == 24_000
    assert smaller.window_tokens == window - 1


@pytest.mark.parametrize("basis", ["provider", "estimator"])
@pytest.mark.parametrize("accepted", [20_000, 100_000])
@pytest.mark.parametrize("anchored", [False, True])
def test_measurement_keeps_conversation_capacity_and_uses_its_counter(
        tmp_path, monkeypatch, basis, accepted, anchored):
    window = 50_000
    built = plan(core(tmp_path), window, facts=RouteFacts(
        conversation_window_tokens=window, accepted_input_tokens=accepted,
        working_reserve_tokens=24_000, basis=basis))
    monkeypatch.setattr(fit, "estimate_context_prompt_tokens", lambda *_a, **_kw: 20_000)
    monkeypatch.setattr("ouroboros.capability_evidence.resolve_main_token_density",
                        lambda *_a, **_kw: (2.0, "fresh_route_usage"))
    result = fit.measure_main_fit(built, built.messages_for("nano"), [], profile="owner_nano",
        rendered_mode="nano", round_id="r1", accepted_input_tokens=accepted,
        anchor=InputAnchor(40_000, 20_000, 1) if anchored else None)
    measure = result.measurement
    assert measure.capacity_total_tokens == measure.window_reading_tokens == window
    assert measure.accepted_input_tokens == accepted
    assert measure.response_reserve_tokens == 24_000
    assert measure.reply_allowance_tokens <= 16_384
    assert measure.measurement_density == (2.0 if basis == "provider" else 1.0)
    assert measure.estimated_input_tokens == (40_000 if basis == "provider" else 20_000)
    assert result.predicted_capacity_miss == (basis == "provider")


@pytest.mark.parametrize("accepted,capacity", [(20_000, 50_000), (100_000, None)])
def test_ordinary_measurement_keeps_its_previous_reserve_density_and_acceptance_rule(
        tmp_path, monkeypatch, accepted, capacity):
    built = plan(core(tmp_path), 50_000)
    monkeypatch.setattr(fit, "estimate_context_prompt_tokens", lambda *_a, **_kw: 20_000)
    measure = fit.measure_main_fit(built, built.messages_for("nano"), [], profile="owner_nano",
        rendered_mode="nano", round_id="r1", accepted_input_tokens=accepted,
        anchor=InputAnchor(40_000, 20_000, 1)).measurement
    assert measure.response_reserve_tokens == 8192
    assert measure.estimated_input_tokens == 40_000
    assert measure.capacity_total_tokens == capacity


@pytest.mark.parametrize("mode", ["max", "low", "nano"])
@pytest.mark.parametrize("window", [0, 50_000, 2_000_000])
def test_ordinary_projection_bytes_match_before_route_facts(tmp_path, monkeypatch, mode, window):
    monkeypatch.setattr(fit, "_route_calibration_ratio", lambda *_a: 1.5)
    captured = replace(core(tmp_path, memory=True), section_root="", dynamic_sections=())
    built = plan(captured, window, mode=mode)
    value = json.dumps({"initial": built.initial_mode, "views": {
        mode: {"messages": built.messages_for(mode), "facts": built.projection(mode).memory_facts}
        for mode in ("max", "low", "nano")}}, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(value.encode()).hexdigest()
    # Exact messages and view facts captured on 34662a93e, including the old cold/density pass.
    expected = {
        ('max', 0): '089a5899dddedd0fab4bc4fca4702d884cf0316fc7ad9468ef0904aa9682dd3d',
        ('low', 0): 'c5d19d762b1460285d30f3c32c90ad9904451ee2144eb4fd30cc13459e843692',
        ('nano', 0): 'b8354d1dfc05fbc56645dd484ad85c474804bd2147a413be01d32ad86a46c2f7',
        ('max', 50000): '4e5f888693c182ead40478e84a9d6f46aebf33822184b6953d354c75e8a85c82',
        ('low', 50000): 'e9c25fee59ca1aa957a701b9aa27f71e7ea1449ac4c8e943be2b584f22cc81d4',
        ('nano', 50000): 'acffdbf6c77d68e8a3e90d0c88621cf9c6a92557db3fcfeb6ef5f2193897c414',
        ('max', 2000000): '3406b4a079f8347de5c31d65f214f6d7d461abeb00ca2e0ae638c4105040b745',
        ('low', 2000000): '54178a81e2c58c2082e111e4b965a0ef3c535088f73887def7df9a83be76d000',
        ('nano', 2000000): 'e089de80190b9ce850bdeb540ff635df947becc913b8e50b074fb8f2809c89aa',
    }
    assert digest == expected.get((mode, window)), (mode, window, digest)
