---
name: gemini_live
description: Gemini native Live audio and image provider for Live Conversation.
version: 0.1.0
type: extension
runtime: python3
entry: plugin.py
plugin_api:
  version: "2.2"
  capabilities: [register_realtime_provider]
permissions: [realtime_provider, net, read_settings]
env_from_settings: [GEMINI_API_KEY]
---

Registers `gemini_live:gemini`. It reuses the preserved Gemini Live transport
mechanisms: native audio, function responses, caption accumulation and explicit
fresh-context renewal. The full ordinary selected history is an ordered role
archive in Gemini's system layout; this is provider packaging, not a separate
memory or summary. After 90 seconds with completed turns, on GoAway, or on a
tool-catalog change it asks the host to refit and reserve a new connection after
pending work finishes. Idle time alone does not rebill unchanged input. Automatic
sliding-window compression is omitted. Resume tokens never replace host context.
Sent audio remains pending until transcription and a turn boundary arrive.
Late output captions retain their audio item's identity; generation completion
alone cannot release that caption barrier. A local schema change wakes the
receiver without cancelling its socket read. Expiry with pending work ends
explicitly; Stop retains any known unfinished text as a partial transcript.
No native Google search tool is enabled; ordinary host tools own effects.
The provider accepts inline images and sends 16 kHz mono PCM input / 24 kHz output.
Installing or granting it does not enable the Live Conversation beta.
