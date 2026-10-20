"""Live Conversation: beta configuration, consumer and approved Talk A surface."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ouroboros.contracts.live_conversation import LiveConfig
from ouroboros.contracts.realtime import RealtimeSetupError

# Calls are off until the owner turns them on; Gemini is the default engine.
DEFAULTS = {"enabled": False, "provider": "gemini_live:gemini", "model": "", "voice": "", "instructions": "",
            "pause": "normal", "interruptions": True, "thinking": "low", "search": True}


def validate(value):
    """The stored configuration as the typed LiveConfig the host reads; unknown keys are refused."""
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
        raise RealtimeSetupError("configuration", "unknown call configuration")
    return LiveConfig(**{**DEFAULTS, **value})


def register(api):
    from .driver import call_tools, run
    from ouroboros.utils import atomic_write_json

    state_dir = Path(api.get_state_dir())
    path = state_dir / "conversation.json"

    async def handle(host):
        await run(host, state_dir=state_dir)

    def configure():
        try:
            return validate(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return validate({})

    async def settings(request):
        from starlette.responses import JSONResponse

        # Ordinary extension settings route; never derives beta enablement from keys.
        if request.method == "POST":
            try:
                config = validate(await request.json())
                if config.enabled:
                    from ouroboros.realtime_setup import loaded_provider
                    loaded_provider(config.provider)
                atomic_write_json(path, asdict(config))
            except (ValueError, RealtimeSetupError) as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse(asdict(configure()))

    api.register_live_conversation("call", handle, configure=configure, call_tools=call_tools(state_dir))
    api.register_route("config", settings, methods=["GET", "POST"])
    api.register_settings_section("conversation", title="Live Conversation", schema={"components": [
        {"type": "markdown", "text": "Calls (beta). Choose and enable an engine skill, grant its API key, then select its registration and native model here. Audio goes to that vendor. Calls use this room's ordinary context and tools."},
        {"type": "form", "route": "config", "method": "POST", "fields": [
            {"name": "enabled", "type": "checkbox", "label": "Calls (beta)", "default": False},
            {"name": "provider", "type": "select", "label": "Engine", "default": "gemini_live:gemini", "options": [
                {"value": "gemini_live:gemini", "label": "Gemini · Google"},
                {"value": "grok_realtime:grok", "label": "Grok · xAI"},
                {"value": "openai_realtime:openai", "label": "OpenAI"}]},
            {"name": "model", "type": "text", "label": "Native model", "default": ""},
            {"name": "voice", "type": "text", "label": "Voice", "default": ""},
        ]},
    ]})
