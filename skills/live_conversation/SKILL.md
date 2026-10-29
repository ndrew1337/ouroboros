---
name: live_conversation
description: Talk with Ouroboros using this room's ordinary memory, tools and controls.
version: 0.1.16
type: extension
runtime: python3
entry: plugin.py
plugin_api:
  version: "2.2"
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

Calls are a beta, off by default. The call skills are on by default. To talk,
turn on Calls (beta), choose an engine and add its API key in Settings → Secrets,
under the keys the skills request.
Gemini, Grok and OpenAI supply the engine, model and voice choices from their
loaded catalogs. The owner's skill auto-grant setting still applies; with it off,
requested permissions await approval in Skills.
Having a key does not turn on calls; an unavailable selection is never replaced.
The selected catalog supplies supported controls, privacy and memory-window
facts. Call instructions and engine choices apply to the next call. Microphone,
speaker, camera and echo/noise/gain choices stay on this client and apply live.

Talk occupies Send while the composer is empty. Text or a file restores Send;
empty Enter never starts a call. Dictation is hidden during the call. Keyboard
returns to the same draft. Hidden rooms and documents keep the call where the
platform allows. End, ordinary Stop, Panic, feature/provider unload, pagehide,
device loss and confirmed Calls-off end it. Failed config reads keep it.

The model reasons over the ordinary selected context and calls ordinary tools.
The host owns memory, canonical roles and history, authority, money and device
leases. Each engine owns its physical wire layout, captions, usage and renewal.
The ordinary shared spending limit is the only money limit; there is no call
time cap or per-call reserve. The driver uses only LiveHost, greets once through
Respond, and renews at a quiet boundary. Its call-local tools remember BCP-47
recognition languages per owner/Presence binding and end after goodbye playback.
The engine-neutral Live calls instructions belong to this feature and precede
the owner's call instructions on every connection.

With providers declaring silent context updates, fresh renewals prepare the next
fitted connection while this one keeps listening; older providers renew sequentially.
At a quiet moment the call switches with new canonical facts intact and no second
greeting. A transport loss without a native handle continues from a fresh fit;
provider refusals still end typed, and repeated unanswered drops remain bounded.

The waiting successor keeps reading and metering into the shared limit; its events
wait for adoption. Overlap has no separate time bound and adds connected seconds
at the captured route tariff. Current options and account scope are checked at
the switch. Only transport failure or a stale fit falls back; typed refusals end
without another attempt. Committed room mail keeps its answer opportunity across
a handoff, and an interrupted answer proves the connection just as a completed one does.

On a weak upload the microphone goes first. Provider frames wait for an empty socket; newer frames replace waiting ones per source. The shared transport measures drainage, sizes only delivery copies to its physical audio-buffer bound, and paces them with spare microphone capacity. Exact original files remain readers. The mind gets one slow-upload fact and one recovery fact; a queued frame is not sight. Requested looks obey the same limit.

Requested looks and streamed frames share one upload owner. Recovery follows a successful full write for every constrained source; a small camera cannot clear a reduced screen. Muting the last microphone releases its reservation, restored before resumed audio. A fresh socket replays requested looks as exact readers until it has measured upload credit. Failed or cancelled offers retain their place ahead of newer facts.
