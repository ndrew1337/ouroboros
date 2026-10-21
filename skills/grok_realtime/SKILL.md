---
name: grok_realtime
description: xAI Grok Voice provider for Live Conversation.
version: 0.1.0
type: extension
runtime: python3
entry: plugin.py
plugin_api:
  version: "2.1"
  capabilities: [register_realtime_provider]
permissions: [realtime_provider, net, read_settings]
env_from_settings: [XAI_API_KEY]
---

Registers `grok_realtime:grok`. The host selects the explicit native model,
ordinary context and function tools, owns authority and reserves spending before
connection. This provider maps xAI's wire, cumulative caption replacements and
outer response usage. Connected-time samples are retained as cumulative evidence;
missing samples and the unreported connection tail remain unknown. No token tariff
or delta sum is substituted. Grok Voice has no image input; Show is unavailable.
Credentials are read only when opening a host-admitted connection. Installing or
granting this provider does not enable the Live Conversation beta.
