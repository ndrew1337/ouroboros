---
name: live_conversation
description: Talk with Ouroboros using this room's ordinary memory, tools and controls.
version: 0.1.0
type: extension
runtime: python3
entry: plugin.py
plugin_api:
  version: "2.1"
  capabilities: [register_live_conversation, register_route, register_settings_section]
permissions: [live_conversation, route, widget, page_module]
contributes:
  - id: call
    provides: conversation
    runtime: client
    label: Live Conversation
    destination: host
    render: {kind: host, entry: ui/index.js}
---

Calls are a beta, off by default. Enable this feature and one of the separate
Gemini, Grok or OpenAI engine skills, grant that engine its credential, then
choose its registration, native model and voice in this skill's settings.
Having a key does not turn on calls; an unavailable selection is never replaced.

Talk occupies Send while the composer is empty. Text or a file restores Send;
empty Enter never starts a call. Dictation is hidden during the call. Keyboard
returns to the same draft. End, ordinary Stop, Panic, feature/provider unload,
leaving the page and losing its device end the call.

The model reasons over the ordinary selected context and calls ordinary tools.
The host owns memory, canonical roles and history, authority, money and device
leases. Each engine owns its physical wire layout, captions, usage and renewal.
The configured reservation is exposure per physical connection, held against
the ordinary budget before dispatch. Unknown audio prices remain unknown and
held. It is not a provider-enforced charge ceiling.
