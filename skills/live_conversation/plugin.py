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


def registered_catalogs(drive_root):
    """Live registrations and current hash-bound grants; no credential values.

    Integration will replace the offer predicate with LiveHost's call_offer.
    The feature still reads catalogs here to build its Settings choices.
    """
    from ouroboros import extension_registry_state as registry
    from ouroboros.skill_loader import grant_status_for_skill

    with registry._lock:
        live = [(key, spec["catalog"], registry._extensions[spec["skill"]].loaded_skill)
                for key, spec in registry._realtime_providers.items()
                if spec["skill"] in registry._extensions and spec["skill"] not in registry._unloading]
    result = []
    for key, catalog, skill in live:
        grants = grant_status_for_skill(Path(drive_root), skill) if skill else {}
        granted = set(grants.get("granted_keys", ()))
        result.append((key, catalog, bool(grants.get("usable")) and set(catalog.credential_settings) <= granted))
    return result


def model_json(model):
    value = asdict(model)
    value["honours"] = sorted(model.honours)
    return value


def configuration_view(config, catalogs):
    selected = next((catalog for key, catalog, _ in catalogs if key == config.provider), None)
    try:
        model = selected.model(config.model) if selected else None
    except RealtimeSetupError:
        model = None
    offered = bool(config.enabled and model and any(key == config.provider and granted for key, _, granted in catalogs))
    return {**asdict(config), "offered": offered, "selected_model": model_json(model) if model else None,
            "catalogs": [{"id": key, "label": catalog.label, "privacy": catalog.privacy,
                          "models": [model_json(item) for item in catalog.models]} for key, catalog, _ in catalogs]}


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

        catalogs = registered_catalogs(api.get_runtime_info()["data_dir"])
        # Ordinary extension settings route; never derives beta enablement from keys.
        if request.method == "POST":
            try:
                config = validate(await request.json())
                if not config.provider:
                    raise RealtimeSetupError("configuration", "Choose an engine.")
                if config.enabled:
                    catalog = next((catalog for key, catalog, _ in catalogs if key == config.provider), None)
                    if catalog is None:
                        raise RealtimeSetupError("provider_unavailable", "The selected engine is not loaded.")
                    model = catalog.model(config.model)
                    if config.voice and config.voice not in {voice.id for voice in model.voices}:
                        raise RealtimeSetupError("voice_unavailable", "Choose a voice from the selected model's catalog.")
                    if "thinking" in model.honours and config.thinking and config.thinking not in model.thinking_levels:
                        raise RealtimeSetupError("configuration", "Choose a thinking level from the selected model's catalog.")
                atomic_write_json(path, asdict(config))
            except (ValueError, RealtimeSetupError) as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse(configuration_view(configure(), catalogs))

    api.register_live_conversation("call", handle, configure=configure, call_tools=call_tools(state_dir))
    api.register_route("config", settings, methods=["GET", "POST"])
    api.register_settings_section("conversation", title="Calls (beta)", schema={"components": [
        {"type": "markdown", "text": "Choose an enabled voice engine. Calls use this room's ordinary memory and tools."},
        {"type": "form", "route": "config", "method": "POST", "fields": [
            {"name": "enabled", "type": "checkbox", "label": "Calls (beta)", "default": False},
            {"name": "provider", "type": "select", "label": "Engine", "default": DEFAULTS["provider"], "options": []},
            {"name": "model", "type": "select", "label": "Model", "options": []},
            {"name": "voice", "type": "select", "label": "Voice", "options": []},
            {"name": "instructions", "type": "textarea", "label": "Instructions for calls"},
            {"name": "pause", "type": "select", "label": "Pause before answering", "options": []},
            {"name": "interruptions", "type": "checkbox", "label": "Speaking interrupts Ouroboros"},
            {"name": "thinking", "type": "select", "label": "Thinking", "options": []},
            {"name": "search", "type": "checkbox", "label": "Built-in search"},
        ]},
    ]})
