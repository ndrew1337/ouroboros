# Answer

**Verdict: INCOMPLETE.** I have now read the whole candidate source, but runtime and visual qualification are missing. Of the 141-file diff (sha256 `b8ace936…`), every hunk was read, tests and config included, along with the DEVELOPMENT and ARCHITECTURE sections my coupling judgments rely on. I found no critical defect: 12 findings, all advisory, plus nits. What is missing is runtime evidence: I ran no tests and inspected no screenshots, and the remote-engine test stand cannot run on Windows. The new evidence changed several earlier claims; each is marked **[changed]** or **[new]** below.

## Contributor review: Remote engines + header Remote switch (separate context, read-only)

**Reviewed:** base `33eb58b4fbca849328426cfcd7a64e054f923741` → head `f835352c5582c5029e6b7648e80a88ac4fb7375c`, tree `6fb02174eea6ca6476122073af80e088ecec4dbf`. The retained diff matches the manifest's 141 files.

**Source read (complete):**
- `CONTRIBUTING.md` and `docs/CHECKLISTS.md`.
- BIBLE P1, P7–P13, Constraints, Emergency Stop.
- DESIGN principles 1–7 and every DESIGN hunk.
- DEVELOPMENT chapters:
  - ch.02 "External facts" and "Provider Independence";
  - ch.03 size and paydown, Pragmatic SOLID, shared data-flow, projection-over-replay, UI disposers;
  - ch.06 Capability placement, Live subagents, Onboarding and Settings surfaces, Transport, LLM call rules;
  - ch.09 Process Custody; ch.10 Platform Abstraction; ch.11 Design System; ch.13 Gateway Boundary; ch.14 marker lanes and test design.
- ARCHITECTURE §3 "Navigation and shared UI contracts" and §6 "Delegated subagents" (Registry, Scheduling, Nannies, Remote engines, Transport, Durable custody, Continuation).
- Callers checked in the candidate:
  - every `following_route` caller, and every `get_light_model` / `_main_model` caller;
  - every `model_role_option(MODEL_ACCOUNTS_KEY|MODEL_CONTEXT_WINDOWS_KEY …)` reader;
  - `recover_model_attempt` callers; `parse_route_spec(reject_unknown=True)` and `parse_configured_subagents`;
  - `scrub_native_custody`; `ClaudexorGateway` headers; `server_control` `attempt()`;
  - `state_snapshots.retire`, `composer_model.changeMain`, `skill_catalogue._reviewed_instructions`, `subagent_work_order._own_body_source` and `session_host_references`, `consolidator._consolidation_route`.

**Not established:**
- No test, network, SSH or provider was run.
- No screenshot was inspected. The browser tests write frames, but none were provided.
- The `host` stand skips on Windows, so remote paths are untested there.

### Coupling questions
```json
[
  {"item": "intent_alignment", "verdict": "PASS", "severity": "advisory", "reason": "Owner asks traced to code: remote engines through core/Claudexor (remote_engines.py, execution_domain.py, remote_workspace.py, remote_stops.py); a computer-icon capsule that opens Accounts and keys > Remote engines when none is saved and otherwise toggles (remote_switch.js remoteView press setup/on/off); On moves Main-following work and new helpers (remote_placement.effective_main, place_delegated). UI wording beyond plain On/Off is reported under further findings."},
  {"item": "forgotten_touchpoints", "verdict": "FAIL", "severity": "advisory", "reason": "web/modules/composer_model.js:437 passes previous = describe(state.main), and state.main is the placed main_route. confirmMainChange (composer_model.js:141-146) then treats a Remote-On Main as lane 'remote' and warns that Main's pinned account becomes Auto when the owner picks a model of the same source on this computer's engine. ouroboros/gateway/owner_effort.py _write_main_route compares the saved routes and keeps that pin (tests/test_remote_switch.py test_a_main_saved_while_on... shows the effective-versus-saved split). Compare against remote_execution.main instead."},
  {"item": "cross_surface_consistency", "verdict": "FAIL", "severity": "advisory", "reason": "What travels to the host is described too narrowly. ouroboros/remote_workspace.py:33 says the data root never travels, and ENGINE_DETAILS (settings_engine_connections.js) and the switch's menu say nothing on this. Yet subagent_work_order.session_host_sources (:653) stages the whole folder of every enabled instruction skill with a current executable review (skill_catalogue.session_skill_reference_dirs, discovered under the canonical data root) on each remote delegated session start whose task input sources are not 'declared'. BIBLE.md and docs/DESIGN.md travel only for own-body work. The run's private copy also sends its untracked, non-ignored files (tests assert a .env travels). This is disclosure wording; no behaviour is wrong."},
  {"item": "cross_surface_consistency", "verdict": "FAIL", "severity": "advisory", "reason": "docs/development/06-rules-by-change-class.md:142 (Live subagents: 'dispatch uses its scheduling snapshot; no second selector, host ranking or substitution') and ARCHITECTURE 6 Registry ('exact selection starts or refuses, without substitution') do not mention that the owner's Remote switch re-places a selected row's engine before the snapshot freezes (remote_placement.place_delegated:256, recorded as snapshot.placement). The ARCHITECTURE 6 Remote engines paragraph documents it; DEVELOPMENT does not."},
  {"item": "regression_surface", "verdict": "FAIL", "severity": "advisory", "reason": "Rollback edge. ouroboros/configured_subagents.py:232-241 parses OUROBOROS_SUBAGENTS with reject_unknown=True, and the base parse_route_spec accepts only {kind, target_id, pin} (shown in the route_spec.py hunk). parse_configured_subagents raises for the whole setting, so once one row saves route.engine/route.workspace, a pre-change body's catalog and review pool fall back to an empty roster with config_error. ENGINE_DETAILS (settings_engine_connections.js:141) says only 'an older version refuses a remote choice'. Source reasoning only: the base was not executed."},
  {"item": "architecture_fit", "verdict": "FAIL", "severity": "advisory", "reason": "Placement is applied in route_choice.following_route (:250), not where model_slots answers Main/Light. With Remote On and an empty Light slot, following_route('light'), the agent-visible route text in tools/control_models.py:84-85 and empty-slot consciousness wakes (consciousness.py:106) move to the remote engine. Light calls through get_light_model/_main_model stay on this computer's engine: consolidator._consolidation_route via subagents._lane_model, review_synthesis.py:204, semantic_dedup.py:107, ui_translation.py:181, project_naming.py:99, presence_runner.py:993. So do Presence places set to 'main' (presence_context.py:875, place_projection.py:110). remote_placement's docstring promises 'a role whose own slot is empty' moves. Advisory because ARCHITECTURE 07 scopes the switch to following_route-based readers; a maintainer could rate it as two answers to one fact."},
  {"item": "implicit_contracts", "verdict": "PASS", "severity": "advisory", "reason": "Checked: engine_ref is bound before the POST and reopened only via bound_gateway (same connection, address, home and store) across custody, documents, model operations and review seats; _request strips _host_engine from a scrub_native_custody deep copy, so transcripts and checkpoints keep it; every recover_model_attempt caller fits the new factory arity (imagegen passes none and refuses remote routes); parse_model_route's ROUTE_KINDS only adds the claudexor@ head and connection_references skips claudexor routes. Pin readers that pass no route model (safety.py:1013, consolidator.py:55, reviewer_slot_config.py:257, onboarding and install presets, the owner_effort writer) read unplaced configured routes, so pin and engine agree."}
]
```

### Further findings (all advisory)
1. **Composer dialog wording.** See forgotten_touchpoints. A Project composer could also save the displayed `claudexor@<engine>::…` as the room's own model, and that room would not return to this computer on Off.
2. **What travels to the host [changed: narrowed].** See cross_surface_consistency. Review seats do not receive skill folders. API helpers stage nothing. A real key that a skill folder holds as ordinary payload would reach the owner's host: that is authorized but undisclosed.
3. **Rollback breaks the Agents roster [changed: validated, wording narrowed].** See regression_surface. Earlier I said "Main reviews alone"; the precise effect is an empty pool with `config_error`.
4. **Two answers to "where does Light/Main-following work run" [changed: confirmed via the consolidator].** See architecture_fit.
5. **Token can reach a local port squatter on first attach [changed: reachability validated].** In `remote_engines._open_tunnel` (:493–524):
   - `_free_port()` (:375) releases a port before ssh binds it.
   - The readiness probe accepts any listener on that port.
   - The token is then written and `handshake()` sends `Authorization: Bearer` (`gateways/claudexor.py:450`).
   - Per OpenSSH's usual order, the local forward binds only after the connection and authentication, so the window is that whole SSH setup. I did not measure this.
   - Only a local process that takes that exact port in the window can trigger it. `test_a_tunnel_is_known_by_its_forwards_custody…` covers only reuse after a forward died.
   - Fix: confirm the spawned ssh process is alive and owns the listener before the handshake.
6. **Capsule can stay on "Saving…".** In `remote_switch.js`, a landed write sets `confirming` (:196), but an answer with `remote_execution: null` is ignored (:306). If `state_view()` keeps failing, "Saving…" persists.
7. **UI and DESIGN choices for maintainers (this is the refinement the PR asks for):**
   - The owner asked for On/Off; the capsule shows "Off" or the engine's label, plus a chevron menu, an "· agents" marker and an amber dot.
   - Its CSS is new and has hardcoded px values.
   - `docs/DESIGN.md:3355` adds "(owner 14.11)", contrary to CHECKLISTS 26 ("new rule text adds no date"), and states more than the owner chose.
8. **Remote run details re-read on every call.** `RemoteEngineGateway.get_run` → `_mirror_facts` (:739, :753) re-lists the run's artifacts and downloads its fact files on each detail read; identical content is only deduplicated after download. How often this happens in practice is not measured.
9. **Module size.** Two new modules start above 1000 lines (`remote_engines.py` 1034, `remote_workspace.py` 1051). They are registered in the size band with rationales and a named split candidate.
10. **What a remote helper can see [changed: corrected].** Only session helpers start on the host. They see the staged copy plus typed inputs and references, not host paths named only in prose; the catalog and menu list refused shapes but not this narrowing. API helpers placed by the switch run as local child tasks: their tools run on this computer and only their model calls go remote, as `rowStatus` says. My earlier review overstated this for API helpers.
11. **Removing or turning off the Remote target while On.** Main and new helpers then refuse with `connection_unavailable` until Remote is pressed Off. The save warning from `settings.py:1115 _retire_unsaved_engine_tunnels` does not mention the switch; only the bar's amber dot signals it.
12. **[new] DEVELOPMENT "Live subagents" not updated.** See the second cross_surface_consistency row.

**Nits:**
- Extra blank lines in `delegate_readonly_inputs.py`, `management_contracts.py` and `remote_engines.py`.
- The process-global `_LOOKED_UP` and `_RETIRE_DUE` dicts are never pruned.
- `_write_private` uses bare `os.open`/`os.replace` instead of the `utils` atomic helpers (a DEVELOPMENT ch.10 preference).
- `remote_switch.js:29 REMOTE_ROW` is a second row object for a key that `settings_rows.js` HAND_WRITTEN already registers.

**Confirmed sound (source):**
- **Emergency Stop:** the remote step runs in a non-awaited `attempt` thread, and owed stops are durable before the Panic receipt. This matches "disclosed as unconfirmed", which the owner accepted.
- **SSH handling:** token file 0600 and absent from every response; BatchMode on, StrictHostKeyChecking on, ControlMaster off; no local shell; remote paths `shlex`-quoted; the destination grammar rejects a leading `-`.
- **Process custody:** the forward via `spawn_supervised`; short commands are tracked and killed at their timeout.
- **Evidence and results:** returned evidence is unpacked with the `tarfile` `data` filter, and the result patch goes through `git apply --check`.
- **Deployments** refuse remote routes. **Version carriers** are untouched. **Endpoint parity:** the endpoint index, router, TypedDicts, JSDoc twins and binding test agree.
- **The stale-Off race** is fixed on both server and client (`state_publication` settings revision; `retireState`), and both directions are covered by `tests/test_state_publication.py` and `web/tests/remote_switch.test.js`.

### Privacy and publication
- **Every line of the diff was read.** It contains no real credential, key, token, IP, private path or e-mail.
  - The only URLs are a pre-existing MiniMax docs link (unchanged context), loopback addresses, and a synthetic UUID namespace (`ouroboros.local`).
  - The secret-looking values are synthetic test constants:
    - `remote-engine-secret-token`;
    - `.env` containing `SECRET=1`;
    - a `private.key` file containing `secret`;
    - `ui-key` / `ui-password`;
    - `/Users/owner/…` and `*.invalid` hosts.
- **The VPS provider name in fixtures is public synthetic data** per the parent: "VDSina", `vds`, `ouroboros@vds`, `/home/ouroboros/…`. Anonymizing it is optional, not required, and not something to change in this frozen candidate. Test docstrings also cite "Astra review (14.11.2026)", which is harmless provenance.
- **Keep out of the PR text:** local workspace and data paths, chat and task ids, the VPS service user, root SSH access, and live-probe errors. The owner's screenshots are private and are not attached.

### Remaining verification limits
- Visual evidence: NOT_RUN, so CONTRIBUTING §4 and CHECKLISTS 11(i) are unmet for a claimed-complete UI change. The PR is a Draft.
- **Tests:**
  - focused, browser (`ui_browser`) and size-ratchet lanes: not run by me;
  - full Python battery: red per the parent (pre-existing), serial lane not run;
  - remote paths on Windows: unexercised.
- Live remote behaviour depends on the owner's own VPS and cannot be reproduced by maintainers. The stand fakes OpenSSH and the engine.
- **Requirements of the remote engine:**
  - a Claudexor with ephemeral scopes;
  - the task-group route for workspace runs;
  - an SSH login user that is the engine's own user.

  Evidence return also needs Python ≥ 3.10.12 or ≥ 3.11.4 locally.
- Upstream divergence is the parent's check. Any rebase or merge makes this review stale.
