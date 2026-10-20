---
name: openai_realtime
description: OpenAI native Realtime audio, images and tools over the host's ordinary selected context.
version: 0.1.0
type: extension
runtime: python3
entry: plugin.py
plugin_api:
  version: "2.1"
  capabilities: [register_realtime_provider]
permissions: [realtime_provider, net, read_settings]
env_from_settings: [OPENAI_API_KEY]
---

Provides `openai_realtime:openai`, an in-process provider for the host's
`RealtimeInput`. The host selects memory, roles, tools, model and output allowance.
This package owns OpenAI's physical field layout, acknowledgement handling and
usage mapping, and declares its route (accounting provider `openai_realtime`, the
Realtime endpoint, model prefix `openai::`) for host fitting and accounting.
Unsupported input fails before a connection opens; nothing is clipped or
reclassified as owner speech. Automatic truncation is disabled. The session's
`max_output_tokens` is `"inf"`: the field takes an integer 1-4096 or `inf`, the
model's maximum, so the host allowance is not written into it. The one probe
response carries its own integer bound, refused offline outside 1-4096.

The host can preview the exact wire plan without credentials or network access.
Execution requires the preview digest and the existing hash-bound grant for
`OPENAI_API_KEY`. The key is read through `PluginAPI.get_settings` only when
opening a connection. The host supplies an event recorder and a total deadline.
An optional, explicit text probe requests one bounded response and runs no tools.
Response usage goes to the host recorder in host axes, beside the raw event, for
host settlement; this package neither prices nor authorizes spending.

`live.py` supplies the separate Live Conversation consumer with native PCM audio,
images, input/output captions and ordinary function calls. It uses the measured
lossless 256000-character text-field fragmentation, retains item role/order,
verifies setup and live item acknowledgements, and disables automatic truncation.
Its audio response bound is the smaller of the host allowance and 4096; the
setup-only text diagnostic above keeps its own explicit probe bound. Server VAD
detects input; this adapter schedules responses after context/tool ACKs and
cancels/truncates generated audio only against the host's playback clock.
Usage retains modality evidence for ordinary accounting, without estimating an
audio invoice from text prices. The feature owns UI and the host owns history,
devices, tools, money and Stop. Granting a key does not enable Calls (beta).
