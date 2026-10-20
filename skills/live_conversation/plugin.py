"""Live Conversation: beta configuration, consumer and approved Talk A surface."""
from __future__ import annotations

import json
import math
from pathlib import Path

from ouroboros.contracts.realtime import RealtimeSetupError

DEFAULTS = {"enabled": False, "provider": "", "model": "", "voice": "",
            "max_seconds": 600, "reservation_usd": 1.0}


def validate(value):
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
        raise RealtimeSetupError("configuration", "unknown call configuration")
    result = {**DEFAULTS, **value}
    if type(result["enabled"]) is not bool:
        raise RealtimeSetupError("configuration", "enabled must be boolean")
    for name in ("provider", "model", "voice"):
        if not isinstance(result[name], str) or len(result[name]) > 160:
            raise RealtimeSetupError("configuration", f"invalid {name}")
    for name in ("max_seconds", "reservation_usd"):
        number = result[name]
        if type(number) not in (int, float) or not math.isfinite(number) or number <= 0:
            raise RealtimeSetupError("configuration", f"{name} must be positive and finite")
    if result["enabled"] and (not result["provider"] or not result["model"]):
        raise RealtimeSetupError("configuration", "choose a loaded engine and its native model before enabling calls")
    return result


def register(api):
    from .driver import run
    from ouroboros.utils import atomic_write_json

    path = Path(api.get_state_dir()) / "conversation.json"

    def configure():
        try:
            return validate(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return dict(DEFAULTS)

    async def settings(request):
        from starlette.responses import JSONResponse

        # Ordinary extension settings route; never derives beta enablement from keys.
        if request.method == "POST":
            try:
                raw = await request.json()
                # The ordinary declarative form serializes numeric inputs as
                # strings. Normalize at this HTTP boundary, then validate.
                if isinstance(raw, dict):
                    for name in ("max_seconds", "reservation_usd"):
                        if isinstance(raw.get(name), str):
                            raw[name] = float(raw[name])
                config = validate(raw)
                if config["enabled"]:
                    from ouroboros.realtime_setup import loaded_provider
                    if not loaded_provider(config["provider"]).get("session"):
                        raise RealtimeSetupError("provider_unavailable", "selected engine has no live session")
                atomic_write_json(path, config)
            except (ValueError, RealtimeSetupError) as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse(configure())

    api.register_live_conversation("call", run, configure=configure)
    api.register_route("config", settings, methods=["GET", "POST"])
    api.register_settings_section("conversation", title="Live Conversation", schema={"components": [
        {"type": "markdown", "text": "Calls (beta). Choose and enable an engine skill, grant its API key, then select its registration and native model here. Audio goes to that vendor. Calls use this room's ordinary context and tools."},
        {"type": "form", "route": "config", "method": "POST", "fields": [
            {"name": "enabled", "type": "checkbox", "label": "Calls (beta)", "default": False},
            {"name": "provider", "type": "select", "label": "Engine", "default": "", "options": [
                {"value": "", "label": "Choose an engine"},
                {"value": "gemini_live:gemini", "label": "Gemini · Google"},
                {"value": "grok_realtime:grok", "label": "Grok · xAI"},
                {"value": "openai_realtime:openai", "label": "OpenAI"}]},
            {"name": "model", "type": "text", "label": "Native model", "default": ""},
            {"name": "voice", "type": "text", "label": "Voice", "default": ""},
            {"name": "max_seconds", "type": "number", "label": "Call time limit (seconds)", "default": 600},
            {"name": "reservation_usd", "type": "number", "label": "Reserve per connection (USD)", "default": 1,
             "help": "Held against the ordinary budget before connecting. Unknown audio prices remain held; this is not a provider-enforced price cap."},
        ]},
    ]})
