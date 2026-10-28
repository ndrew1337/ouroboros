"""Provider-measured audio retention and response accounting, on fake wires."""
from __future__ import annotations

import asyncio

from ouroboros.contracts.live_conversation import LiveOptions
from ouroboros.contracts.realtime import USAGE_AXES
from tests.test_gemini_live_boundaries import deny_network  # noqa: F401
from tests.test_gemini_live_boundaries import gemini as gemini_fixture, audio_content, make_session, receive

gemini = gemini_fixture


def usage(audio, text=100):
    return {"promptTokensDetails": [{"modality": "TEXT", "tokenCount": text},
                                    {"modality": "AUDIO", "tokenCount": audio}]}


async def turn(session, wire, raw):
    await receive(session, wire, audio_content())
    return await receive(session, wire, {"serverContent": {"turnComplete": True}, "usageMetadata": raw})


def test_modality_axes_status_and_missing_values(gemini):
    async def run():
        session, wire = make_session(gemini)
        try:
            await session.open()
            await session.receive()
            raw = {**usage(31), "promptTokenCount": 9999, "responseTokenCount": 999,
                   "promptTokensDetails": usage(31)["promptTokensDetails"] + [
                       {"modality": "IMAGE", "tokenCount": 7}, {"modality": "VIDEO", "tokenCount": 11}],
                   "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 23},
                                             {"modality": "TEXT", "tokenCount": 0}],
                   "cachedContentTokenCount": 42, "thoughtsTokenCount": 9}
            events = await turn(session, wire, raw)
            done = next(e.data for e in events if e.kind == "response_done")
            assert done["usage"] == dict(text_input_tokens=100, audio_input_tokens=31,
                image_input_tokens=7, video_input_tokens=11, text_output_tokens=0,
                audio_output_tokens=23, cached_input_tokens=42, thinking_tokens=9)
            assert set(done["usage"]) <= set(USAGE_AXES)
            assert done["raw_usage"] == raw
            assert done["response_id"] and done["status"] == "completed"
            assert done["continues"] is False
            events = await turn(session, wire, {"promptTokenCount": 10, "responseTokenCount": 4})
            done = next(e.data for e in events if e.kind == "response_done")
            assert done["usage"] == {}  # Aggregate counts cannot reveal modality.
            await receive(session, wire, audio_content())
            await receive(session, wire, {"serverContent": {"interrupted": True}})
            events = await receive(session, wire, {"serverContent": {"turnComplete": True},
                "usageMetadata": {"cacheTokensDetails": [{"modality": "AUDIO", "tokenCount": 8}]}})
            done = next(e.data for e in events if e.kind == "response_done")
            assert done["status"] == "interrupted"
            assert done["usage"] == {"cached_input_tokens": 8}
        finally:
            await session.close()
    asyncio.run(run())


def test_retention_uses_learned_audio_growth_not_an_idle_clock(gemini):
    gemini.HELD_AUDIO_SPANS["gemini-3.8-live"] = 2500
    async def run():
        session, wire = make_session(gemini)
        try:
            await session.open()
            await session.receive()
            assert not any(e.kind == "renew" for e in await turn(session, wire, usage(600)))
            assert not any(e.kind == "renew" for e in await turn(session, wire, usage(1200)))
            assert not any(e.kind == "renew" for e in await turn(session, wire, {"promptTokenCount": 90000}))
            events = await turn(session, wire, usage(1900))  # 1900 held + 700 observed growth >= 2500.
            renew = next(e.data for e in events if e.kind == "renew")
            assert renew == {"within_s": 30, "reason": "audio_span", "resume": False}
            assert not any(e.kind == "renew" for e in await turn(session, wire, usage(2200)))
            assert not hasattr(gemini, "RENEW_AFTER_SECONDS")
        finally:
            await session.close()
    asyncio.run(run())


def test_observed_drop_lowers_the_route_span_for_the_next_fresh_session(gemini):
    async def run():
        session, wire = make_session(gemini)
        try:
            await session.open()
            await session.receive()
            assert not any(e.kind == "renew" for e in await turn(session, wire, usage(700)))
            assert not any(e.kind == "renew" for e in await turn(session, wire, usage(1100)))
            events = await turn(session, wire, usage(500))
            assert next(e.data["reason"] for e in events if e.kind == "renew") == "audio_drop"
        finally:
            await session.close()
        fresh, fresh_wire = make_session(gemini)
        try:
            await fresh.open()
            await fresh.receive()
            assert not any(e.kind == "renew" for e in await turn(fresh, fresh_wire, usage(500)))
            events = await turn(fresh, fresh_wire, usage(700))
            assert next(e.data["reason"] for e in events if e.kind == "renew") == "audio_span"
        finally:
            await fresh.close()
    asyncio.run(run())


def test_thinking_intermediate_response_does_not_release_context_or_reuse_charge_id(gemini):
    async def run():
        session, wire = make_session(gemini, LiveOptions(), model="gemini-3.8-live-extended-thinking")
        try:
            await session.open()
            await session.receive()
            await receive(session, wire, audio_content())
            events = await receive(session, wire, {"serverContent": {"turnComplete": True, "interactionStatus": "IN_PROGRESS"},
                "usageMetadata": usage(100)})
            first = next(e.data for e in events if e.kind == "response_done")
            assert first["continues"] is True
            assert not session.renewal_ready
            events = await turn(session, wire, usage(200))
            last = next(e.data for e in events if e.kind == "response_done")
            assert first["response_id"] != last["response_id"]
            assert last["continues"] is False
            assert session.renewal_ready
            assert not any(e.kind == "response_done" for e in await receive(session, wire,
                {"serverContent": {"turnComplete": True}}))
        finally:
            await session.close()
    asyncio.run(run())


def test_thinking_idle_without_more_output_closes_the_existing_receipt(gemini):
    async def run():
        session, wire = make_session(gemini, model="gemini-3.8-live-extended-thinking")
        try:
            await session.open()
            await session.receive()
            await receive(session, wire, audio_content())
            events = await receive(session, wire, {"serverContent": {"turnComplete": True, "interactionStatus": "IN_PROGRESS"},
                "usageMetadata": usage(100)})
            first = next(e.data for e in events if e.kind == "response_done")
            events = await receive(session, wire, {"serverContent": {"turnComplete": True, "interactionStatus": "IDLE"}})
            last = next(e.data for e in events if e.kind == "response_done")
            assert last == {**first, "continues": False}  # Same settlement identity, only interaction state changes.
            assert session.renewal_ready
            assert not any(e.kind == "response_done" for e in await receive(session, wire,
                {"serverContent": {"turnComplete": True, "interactionStatus": "IDLE"}}))
        finally:
            await session.close()
    asyncio.run(run())


def test_first_process_session_has_no_assumed_audio_limit(gemini):
    async def run():
        session, wire = make_session(gemini)
        try:
            await session.open()
            await session.receive()
            for count in (600, 1900, 2500, 10000):
                assert not any(e.kind == "renew" for e in await turn(session, wire, usage(count)))
            assert not gemini.HELD_AUDIO_SPANS
        finally:
            await session.close()
    asyncio.run(run())


def test_compression_explains_only_a_drop_after_the_trigger(gemini):
    async def run():
        for model in ("gemini-3.8-live", "gemini-3.8-live-extended-thinking"):
            for previous_prompt, explained in ((92000, True), (91999, False)):
                gemini.HELD_AUDIO_SPANS.clear()
                session, wire = make_session(gemini, model=model)
                observed = []
                session.observe = lambda direction, raw: observed.append((direction, raw))
                try:
                    await session.open()
                    await session.receive()
                    config = wire.sent[0]["setup"]["contextWindowCompression"]
                    assert config == {"triggerTokens": 92000, "slidingWindow": {"targetTokens": 82000}}
                    await turn(session, wire, {**usage(3000), "promptTokenCount": previous_prompt})
                    events = await turn(session, wire, {**usage(500), "promptTokenCount": 81000})
                    assert any(e.kind == "renew" for e in events) is not explained
                    assert (model in gemini.HELD_AUDIO_SPANS) is not explained
                    if explained:
                        assert any('compression_explains_drop' in raw for _, raw in observed)
                        assert session.audio_growth == 0
                    else:
                        assert gemini.HELD_AUDIO_SPANS[model] == 3000
                finally:
                    await session.close()
    asyncio.run(run())
