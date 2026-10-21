# 13. External Skills Layer

Discovery, review, grants, dependencies and enablement stay separate: a toggle grants no execution authority. Context snapshots: §6; protocol: `CREATING_SKILLS.md`.

### Skill gates

Discovery is not trust; review/ON prove no readiness. Paths: §1. Install → preflight → digest-bound review/acceptance → grants → dependencies → enablement → execution; extensions register (§10 invariant 8). Enabled-peer conflicts are symmetric. `skill_lifecycle_queue.py` owns mutations; `answer_or_job` awaits completion unless the client requests early replies (older UI/dictation remain terminal). Enable survives a same-job grant; grant/setup results stay separate (CLIENT_CONTRACT §11). Panels lend the lane, persisting only for installed bytes. Reviews stay private; UI marks dead-process/stale-heartbeat jobs interrupted without writes. Lists heal nothing. Post-start ordinals under the lifecycle lock explain retries, never authorize review.

Each request resolves one canonical directory through the full loader. Conflicts use fresh manifest/enablement peers (`skill_peer_inventory.py`), never payload hashes/partial objects. Both scans use `skill_loader._native_platform_visible`: foreign built-ins stay hidden except re-seeded Unix remote on Windows (§2). Provenance survives OFF for later intentional ON, not execution authority. `skill_conflicts.py` gives loaded/peer objects one duck-typed verdict. Both refresh after slow lifecycle work: cross-process disable/revoke/restored-mtime edits apply next request; valid manifests retain conflicts with unreadable payloads.

### Skill review authority

A reviewer's configured rights separately govern checks of untrusted code in its private copy: Full is not an OS sandbox, and checking grants no skill activation.

Host acceptance binds exact bytes: trusted source, Cyber-only `self_check`, owner Skip or selected-task owner edit. Requested edits use `skill_owner_action` Skip with `owner_source`; edits stale acceptance. Review-source blockers stay off after Advisory finish. Other authors may accept substantive feedback or an exact terminal job/hash with partial feedback/disclosed unavailability, run preflight and save `author_disposition.review_reference`. A live silent critic is not unavailable. `SkillReviewState.gate_for` preserves critics: Blocking needs fresh approval; Cyber keeps P0/P3 judgment. Preflight failure persists as PENDING, never advisory-overridable BLOCKERS. Publication: §6.

### Telegram built-in

Thoughts/completion read bounded chat/progress windows from `state/dialog.sqlite`; legacy log paths are addresses only. Import/segment/interval failures never initialize/advance notification seen IDs; thoughts keep preview limits.

`skills/telegram/` runs the in-process bridge and supervised companion (sidecar/tunnel/menu rollback/heartbeat/singleton). Text needs no tunnel. `skill_builtin.py` requires grants; Colab enables after executable install and permitted auto-grants. `data/state/skills/telegram/` holds binding/companion/rollback/cursors/tunnel cache/bounded derived message refs; map failures warn without stopping delivery. Files enter `/chat/inject` → `data/uploads`; quizzes, `/chat/decision`. Replies carry row/work facts, not routing; notices name work, not unseen Main answers. Earlier same-chat owner messages get native replies; Continue stays separate. Relays succeed silently or fail in one line. Oversized files link in-app/to an active Mini App without starting tunnels or publishing URLs. `lib/telegram_i18n.py` uses `OUROBOROS_UI_LANGUAGE` (§7), preserving host lines; `/language` uses Host `/ui/language`. Completion toggles terminal/Main notices (Off consumes IDs); replay enriches `completion_notice` (§3), not text. Silent Mode/replies/files/quizzes/salvage stay independent. Events only wake one `TypingCue` over `ouroboros/gateway/reply_preparation.py` (DESIGN §9). `TELEGRAM_PROXY`: Telegram API only, otherwise direct; locked `httpx[socks]`. Limits/proxy forms: `skills/telegram/SKILL.md`.

`skill_catalogue.py`: `list_skills` pages whole compact records (disabled/unreviewed/broken/colliding included), counting omissions. Snapshots bind membership, not atomic pages. Named reads give full diagnostics and `manifest.read` addresses for `SKILL.md`/`skill.json`, not bodies. Readable manifests survive unreadable payloads; unparsed/colliding ones have no address. Unknown names: `found:false`; oversized details: generic result source. `model_experience` enters bounded context; `SkillManifestError.fix_hint` guides repair. Delegate index: §6.

### Sources, installs and dependencies

`skill_sources.py` defaults to Official trusted, Community review; trust edits affect future installs. Git caches (`state/skill_sources/`) read github.com HTTPS without the configured GitHub token (this computer's git credentials still apply), retrying only auth refusals once with that token alone, without a credential helper; refusal cannot erase a stored login. Other URLs use host credentials. Hubs supply entries/digest endorsements/categories; repos, manifest folders. `skill_source_install.py` verifies releases/records basis: `check:review` buys a panel, Cyber-only `self` records why these bytes may run, trusted sources skip review. HTTP: `gateway/skill_source_contracts.py`.

Updates compare candidate/optional local digests: Replace keeps `.previous`, Keep skips, Merge starts an owner task. `pending_update` names the candidate release; payload/lifecycle snapshots are retained separately for review-basis updates. Completion/Skip/startup settle in the lane: accept executable bytes or restore/reconcile; review/preflight/landing/dependency failures record `update_rejected`. Startup spares live reviews. Pre-update records recover interrupted landings; superseded installs discard snapshots. Partial restores keep evidence, never replacing twice. No-review updates bind `owner_skip` to bytes. Starters accept any source (absent/blank: defaults; `[]`: none); `OUROBOROS_STARTER_SKILLS_TURN_ON` defaults true (§7). Built-ins precede Git fetches.

ClawHub archives land atomically with per-file hashes. `skill_dependencies.ensure_skill_dependencies` readies each runnable request's packages under byte acceptance; `removal_action` names removal. Accepted declarations authorize literal build/check argv (`marketplace/install_specs.py`); manual instructions remain guidance. Caches: `state/skills/<name>/dependency_cache`; results: `deps.json`. Missing checks stay unknown; drift invalidates readiness. `provenance.py` clears only the local receipt. Runtimes: `tools/skill_exec.py`, `CREATING_SKILLS.md`.

### Tool result views

Composition/`PRODUCER_RESULT_SOURCE_JSON` vs `FULL_RESULT_SOURCE_JSON`: `tools/tool_result.py`, `loop_tool_execution.py`.

### Extension import and isolation

`extension_import_staging.py` shares complete `__extension_imports/` trees by content/runtime/dependency identity. PID references last until teardown. The cross-process imports lock sits beside disposable state, preserving its inode across deletion. Local delete unloads and removes payload and the entire state directory under the lifecycle lock, without a tombstone or deferred cleanup. Same-name installs across sources preserve shared state/grants.

Per-call children reuse the loader/scrubbed env, proxying tools/routes/WS/UI/settings/companion descriptors; persistent subscriptions/tasks need in-process or companion lifecycle. Support follows the capability matrix, not platform names. `extension_catalog.json` caches success for exhaustive `catalog_inputs` only: runtime, host revision, grants and declared settings/env are hashed, values never stored. Other dependencies retain the catalog child; bundled payloads never opt in implicitly. The in-process path enforces namespacing, permissions, per-skill tracking and atomic unload.

`skill_repair_admission.py` admits managed repair against exact bytes (§6). Bucket/name produces `skill_repair`, promoted to `normal`: payload selection, not a reduced tool profile. Installed payloads need no Git copy; review never forces unload for repair.

### Live Conversation and provider packages (PluginAPI 2.1)

Features register `LiveConfig`/call tools, owning media/UI; providers register route/session/catalog, owning wire/usage/route facts. `call_offer` checks calls enabled, provider and credential grants.

`gateway/call_ws.py` admits an owner turn with a `call:<call_id>:open` host fact, answered once by `Respond`. `LiveHost` owns ordinary history/tools/mailbox/money/Stop with process-local authority. `realtime_setup.py` refits current history/schemas/instructions per connection through the ordinary ladder/physical guard. Acceptance cannot raise/invalidate windows. Provider windows use text density; estimator windows, raw chars/4. Grok usage excludes instructions, never calibrating density. Route reserves survive Nano's reply cap; unresolved overflow refuses typed.

`pricing_catalog.py` warms LiteLLM entry/digest/date before audio; `live_usage.py` reserves responses and settles modality axes/status in the shared ledger. Per-connection connected seconds span idle/response/close samples; only growth is charged, even after Stop/budget exhaustion. Responses survive resume; IDs dedupe per call. Unknown usage/prices stay unknown; quotes never silently change. The shared limit refuses the next response.

Providers preserve host facts and await setup ACKs before greeting. OpenAI disables truncation and maps the response allowance to its native field. Renewal refits at a quiet transcription/tool/playback/caption boundary. Silent results open no response. Stop ends calls; no diagnostic probe remains.

Call tools serialize execution and pre/post-effect history; `ToolBatch` carries silent ids. Captions stay durable; completed turns checkpoint. `observe_context` labels peer facts. Held input, growth and reserve drive renewal; audio-drop policy stays in the provider.

Presence keeps gates/provenance; browser leases: §3. Reload waits; disable cancels. Host joins sockets/tools at teardown. Contract: `CREATING_SKILLS.md`.
