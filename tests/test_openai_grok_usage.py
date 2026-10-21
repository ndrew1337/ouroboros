"""Native normalized receipts reach the real host pricer and ledger."""
import copy

import pytest

from ouroboros import live_usage, pricing_catalog, usage_accounting
from tests.test_grok_openai_protocol import providers as providers, deny_network as deny_network, make
from tests.test_live_usage import quote as quote, reserve


def openai_receipt():
    return {"input_token_details": {"text_tokens": 10, "audio_tokens": 20, "image_tokens": 30, "cached_tokens": 0},
            "output_token_details": {"text_tokens": 40, "audio_tokens": 50, "reasoning_tokens": 0}}


@pytest.mark.parametrize("missing", [None, "image_tokens", "audio_tokens"])
def test_openai_supported_axes_reach_pricer_and_ledger(providers, quote, missing):
    root, tariff, _ = quote
    raw = openai_receipt()
    if missing:
        raw["input_token_details" if missing == "image_tokens" else "output_token_details"].pop(missing)
    adapter, _ = make(providers, "openai")
    done, = adapter.convert({"type": "response.done", "response": {"id": "r", "status": "completed", "usage": raw}})
    usage = done.data["usage"]
    cost, evidence = live_usage._price(tariff, usage, done.data["raw_usage"])
    if missing:
        axis = "image_input_tokens" if missing == "image_tokens" else "audio_output_tokens"
        assert axis not in usage and cost is None
        assert "unknown_" + axis in evidence["unpriced"]
    else:
        assert cost == pytest.approx(.00169) and not evidence["unpriced"]
    assert usage["video_input_tokens"] == 0
    attempt = reserve(root, tariff)
    live_usage.settle_live_response(attempt, usage=usage, raw_usage=done.data["raw_usage"], status=done.data["status"])
    row, = usage_accounting.read_usage_records(root)
    assert row["state"] == "settled"
    assert row["cost_usd"] == (pytest.approx(.00169) if missing is None else None)


def test_openai_missing_tariff_and_unreported_billable_axes_stay_unknown(providers, quote):
    _, tariff, _ = quote
    tariff = copy.deepcopy(tariff)
    tariff["entry"].pop("output_cost_per_audio_token")
    normalize = providers["openai"].module._usage
    usage = normalize({"usage": openai_receipt()})
    cost, evidence = live_usage._price(tariff, usage, openai_receipt())
    assert cost is None and "output_cost_per_audio_token" in evidence["unpriced"]
    unknown = normalize({})
    assert unknown["video_input_tokens"] == 0
    assert "text_input_tokens" not in unknown and "audio_output_tokens" not in unknown
    assert live_usage._price(tariff, unknown, {})[0] is None


def test_grok_absent_image_video_are_zero_while_billable_usage_stays_reported(providers, quote):
    root, _, _ = quote
    adapter, _ = make(providers, "grok")
    adapter.connected_seconds = 60.0  # fixed connection watermark for the pricing fixture
    raw = {"input_token_details": {"text_tokens": 10}, "output_token_details": {"audio_tokens": 5}}
    done, = adapter.convert({"type": "response.done", "response": {"id": "r", "status": "completed", "usage": raw}})
    usage = done.data["usage"]
    assert usage["image_input_tokens"] == usage["video_input_tokens"] == 0
    assert usage["text_input_tokens"] == 10 and usage["audio_output_tokens"] == 5
    assert "audio_input_tokens" not in usage and "text_output_tokens" not in usage
    tariff = pricing_catalog.get_catalog_quote("xai/grok-test", provider="grok_realtime", drive_root=root, allow_live_fetch=False)
    cost, evidence = live_usage._price(tariff, usage, done.data["raw_usage"])
    assert cost == pytest.approx(.05) and not evidence["unpriced"]
