# Answer

**Verdict: INCOMPLETE.** I found no critical defect in the source. I found 11 advisory findings and 2 nits. The verdict is incomplete because I ran no tests, read most test bodies only by name, and there are no inspected screenshots of the new UI. CONTRIBUTING and CHECKLISTS 11(i) expect inspected UI evidence for a visible change, and the review rules say not to report PASS without required material. Nothing here should block publishing a Draft PR that discloses these points.

Writing a plan file was blocked here (no Write tool and no ExitPlanMode), so the review is below in full.

---

## Contributor review: Remote engines + header Remote switch (separate context, read-only)

**Reviewed range:** base `33eb58b4fbca849328426cfcd7a64e054f923741` → head `f835352c5582c5029e6b7648e80a88ac4fb7375c`, tree `6fb02174eea6ca6476122073af80e088ecec4dbf`. Retained patch sha256 `b8ace93647a6bb96ab95f1089cc22d4161d474b4c7c80a6b6e6ef2c0206a37d0` (1,027,212 B); the manifest lists 141 files.

**Owner's ask, as I checked it against the code:**
- Route remote work through the core and Claudexor.
- A header button with a computer icon, On/Off; with no remote engine set up it opens the Remote engines settings, otherwise it toggles.
- On means "Main and new Claude/Codex agents".
- Publish as a PR that asks maintainers for UI refinement.

### What I read and checked
- **Read in full:** `CONTRIBUTING.md`, `docs/CHECKLISTS.md`.
- **Read in part:**
  - BIBLE: P1, P7 and P9, the Constraints section and the Emergency Stop invariant.
  - DESIGN: principles 3–7, plus every DESIGN hunk in the diff.
- **Diff:**
  - Read every docs hunk, every non-test Python hunk, all of `web/modules/*` and the `style.css` hunk.
  - The new files `remote_engines.py`, `remote_placement.py`, `remote_stops.py`, `remote_workspace.py`, `gateway/engine_connections.py`, `remote_switch.js` and `settings_engine_connections.js` were read whole.
- **Callers checked in the candidate:**
  - every caller of `following_route` and `configured_references`, and of `get_light_model` / `_main_model`;
  - `recover_model_attempt` callers, and `parse_route_spec(reject_unknown=True)` callers;
  - `provider_connections.connection_references`, `server_control.execute_panic_stop` `attempt()`, `state_snapshots.retire`;
  - `composer_model.changeMain`, `server_runtime.has_remote_provider`, `consciousness.model_route`;
  - `presence_context` and `place_projection` model reads, `control_models.resolve_model`.
- **Confirmed sound:**
  - **Emergency Stop:**
    - The remote step runs in its own non-blocking Panic thread and never delays local kills.
    - Stops still owed on remote work are written durably before the Panic receipt.
    - A boot never waits on SSH.
    - This matches the BIBLE wording "killed … or disclosed as unconfirmed", and the owner accepted the disclosed limitation in the recorded local-trial choice.
  - **Token and SSH handling:**
    - The token file is 0600 and never appears in any response.
    - SSH runs with `BatchMode` and `StrictHostKeyChecking=yes`, and `ControlMaster` is off.
    - Remote paths are quoted with `shlex`, and the destination grammar rejects a leading `-`.
  - **Process custody:** the forward is started through `spawn_supervised`; short commands are tracked and killed at their timeout.
  - **Evidence return:**
    - Unpacking uses the `tarfile` `data` filter, and any member outside `.review-evidence/` refuses the whole return.
    - Without that filter, nothing comes back.
  - **Deployments:** managed deployments refuse remote routes.
  - **Version carriers:** untouched, so the change is version-neutral.
  - **Endpoint parity:** the endpoint index, router, contracts, `api_client`, the JS typedefs and the binding test agree.
  - **Remote reviewer seats** keep their engine all the way through to dispatch.
  - **Account pins** are applied per engine everywhere they are read.
  - **The stale-Off race** reported earlier is addressed: a settings save retires older `/api/state` productions (`state_publication.py`), and the client retires its in-flight reads.
  - **Capability placement in core** is owner-fixed (the recorded choice was core/Claudexor).
- **Not done:**
  - I did not read `DEVELOPMENT.md` / `ARCHITECTURE.md` sections beyond the touched chapter hunks.
  - I read about 130 new test functions by name only; the one test body I read was `test_the_remote_engine_routes_answer_their_published_shapes`.
  - No tests, network, SSH, provider calls or screenshots were run or inspected.

### Coupling questions
```json
[
  {"item": "intent_alignment", "verdict": "PASS", "severity": "advisory", "reason": "Owner asks traced to code: core/Claudexor remote engines (remote_engines.py, execution_domain.py), a computer-icon capsule that opens Accounts and keys › Remote engines when none is saved and otherwise toggles (remote_switch.js remoteView press setup/on/off), On scoped to Main plus new session and subscription helpers (remote_placement.effective_main, place_delegated). UI wording beyond plain On/Off is reported under further findings."},
  {"item": "forgotten_touchpoints", "verdict": "FAIL", "severity": "advisory", "reason": "web/modules/composer_model.js:437 passes previous = describe(state.main), and state.main is the placed main_route. confirmMainChange (composer_model.js ~141-146) therefore treats a Remote-On Main as lane 'remote'. When the owner picks a model on this computer's engine, the dialog says Main's pinned account becomes Auto, but ouroboros/gateway/owner_effort.py _write_main_route compares the saved route and keeps the pin. Compare against remote_execution.main (the saved route) instead."},
  {"item": "cross_surface_consistency", "verdict": "FAIL", "severity": "advisory", "reason": "What travels to the remote host is understated. The remote_workspace module docstring says the data root never travels, and ENGINE_DETAILS (settings_engine_connections.js) and the switch's menu say nothing on this. Yet subagent_work_order.session_host_sources stages BIBLE.md, docs/DESIGN.md and every reviewed instruction-skill folder (skill_catalogue.session_skill_reference_dirs, under the data root) on every remote start whose inputs are not declared. Name these in the docstring and in the Settings text."},
  {"item": "regression_surface", "verdict": "FAIL", "severity": "advisory", "reason": "Rollback edge: ouroboros/configured_subagents.py:232-241 parses OUROBOROS_SUBAGENTS routes with reject_unknown=True, and the base version does not know the new route.engine / route.workspace keys. After one remote session row is saved, rolling back to a pre-change body makes the whole Agents roster a config_error: no helpers, and an empty review pool. ENGINE_DETAILS only says 'an older version refuses a remote choice'. State this effect, and possibly make Remove-before-rollback the documented path."},
  {"item": "architecture_fit", "verdict": "FAIL", "severity": "advisory", "reason": "The switch is applied in route_choice.following_route (route_choice.py:250), not where Main and Light are read (model_slots._main_model / get_light_model). With Remote On and an empty Light slot: following_route('light') and the agent-visible text in tools/control_models.py:84-85 say Light runs on the remote engine, and empty-slot consciousness wakes move too (consciousness.py:106). But Light calls made through get_light_model stay on this computer's engine: review_synthesis.py:204, semantic_dedup.py:107, ui_translation.py:181, project_naming.py:99, presence_runner.py:993. So do Presence places set to 'main' (presence_context.py:875, place_projection.py:110). The remote_placement docstring says 'a role whose own slot is empty' moves. Either apply the placement where those readers get the model, or narrow following_route and the docs to Main-following turns. Advisory because ARCHITECTURE 07 lists only following_route-based readers; maintainers could reasonably rate it critical as two answers to one fact."},
  {"item": "implicit_contracts", "verdict": "PASS", "severity": "advisory", "reason": "Checked: engine_ref is bound before the POST and reopened only through bound_gateway (same connection, address, home and store) across delegate custody, documents, model operations and review seats; every recover_model_attempt caller fits the new factory arity; parse_model_route's switch to ROUTE_KINDS only adds the claudexor@ head; connection_references skips claudexor routes; following_moved and route_moved are handled in loop_round_limits; the publication settings_revision is honoured in refresh and in current."}
]
```

### Further findings (all advisory)
1. **Composer dialog wording:** see forgotten_touchpoints. In a Project composer, picking the shown Main route can also save `claudexor@<engine>::…` as the room's own choice, so turning Remote Off would not bring that room back.
2. **What travels to the host:** see cross_surface_consistency. It also has a cost: every remote start copies all reviewed instruction-skill folders over SSH. A real key that a skill folder holds as ordinary payload would land on the owner's host, which is authorized, but the Settings text never discloses it.
3. **Rollback breaks the Agents roster:** see regression_surface.
4. **Two answers to "where does Light/Main-following work run":** see architecture_fit.
5. **Token could reach a local port squatter (`remote_engines._open_tunnel`):**
   - A local port is chosen with `_free_port()`, then the readiness probe accepts any listener on `127.0.0.1:<port>`. Only after that are the token written and the bearer handshake sent.
   - If another local process takes the port in that window, `ExitOnForwardFailure` makes ssh exit, but the probe may already have connected to that process, which would then receive the engine token.
   - `_alive()` guards exactly this for reuse, but not on first open. The trigger is a local process, so this is advisory.
   - Fix: confirm the ssh process is alive and owns the listener before the handshake.
6. **Capsule can stay on "Saving…" (`remote_switch.js` `update()`):** after a write lands, a `remote_execution: null` answer (projection unread) is ignored and `confirming` never clears. If the projection keeps failing, the capsule says "Saving…" indefinitely. Clear it on the first post-write state, or show "unknown".
7. **UI and DESIGN choices for the maintainers to rule on (this is what the PR asks them for):**
   - The owner asked for "On/Off". The capsule shows "Off" or the engine's label, adds a chevron menu, an "· agents" marker and an amber dot, and has its own CSS with hardcoded px values instead of a named shared capsule primitive.
   - The added DESIGN text "Remote (owner 14.11) …" adds a date, which CHECKLISTS 26 forbids in new rule text ("new rule text adds no date"), and it states more than the owner chose.
8. **Fact files re-read on every run read (`RemoteEngineGateway.get_run` → `_mirror_facts`):**
   - Each read of a live run lists its artifacts and downloads its fact files through the tunnel again; identical content is only deduplicated after download.
   - This happens on observation, reconcile and collection paths, and there is no reuse when the listing is unchanged.
9. **Module size (BIBLE P7):** two new modules start above 1000 lines (`remote_engines.py` 1034, `remote_workspace.py` 1051). They enter the size band with "entered the band from 1000 lines" rationales; a split candidate is named for `remote_workspace`.
10. **Remote helpers see less than local ones:** with Remote On, every fresh session helper, and every subscription-API helper on this computer's engine, starts on the host. Those include read-only analysis helpers. They can read only the staged copy plus the typed inputs and references. Host paths named only in prose (memory files, the operator's home) are not available there, and neither the catalog facts nor the menu says so.
11. **Removing or turning off the Remote target while On:** Main then refuses with `connection_unavailable` until Remote is pressed Off. The save warning from `_retire_unsaved_engine_tunnels` does not mention the switch; only the bar's amber dot signals it.

**Nits:**
- Stray extra blank lines in `delegate_readonly_inputs.py`, `management_contracts.py` and `remote_engines.py`.
- The process-global `_LOOKED_UP` and `_RETIRE_DUE` dicts are never pruned (each grows only per unknown start or unconfirmed removal).

### Privacy and publication hazards
- No secrets, tokens, keys, real IPs, private hostnames or owner paths in the diff; the only `/Users/…` strings are the synthetic `/Users/owner/…`.
- **The test fixtures name the owner's real VPS provider.** The label "VDSina", the alias `vds` and `remote_root` `/home/ouroboros/runs` appear in:
  - `tests/test_remote_switch.py`;
  - `tests/test_remote_switch_browser.py`, including its assertions;
  - `web/tests/remote_switch.test.js`.

  Replace them with a neutral label such as "Build box" before publishing.
- Keep these out of the PR body and the review evidence, or make them generic:
  - local workspace and data paths, chat and task ids;
  - the live-probe details: the VPS service user, root SSH access, the bwrap sandbox error.

  The owner's screenshots are private and are not attached.

### Integration limits to disclose in the PR
- The upstream `ouroboros` branch has moved; the parent is checking divergence separately. Any rebase or merge makes this review stale.
- **Python tests:**
  - The full battery is red (pre-existing per the parent); the serial lane did not run.
  - The browser lanes (`test_remote_*_browser.py`) are opt-in.
  - None of this was verified here.
- **Visual evidence is NOT_RUN** (CONTRIBUTING §4 requires it for a visible UI change).
- Live qualification depends on the owner's own VPS and is not reproducible by maintainers. `tests/_remote_engine_stand.py` is a fake OpenSSH and engine.
- **Requirements of a remote engine:**
  - Claudexor with ephemeral scopes.
  - The task-group route, for runs in a declared remote workspace.
  - A login user that matches the engine's own user.
  - Evidence return also needs Python ≥ 3.10.12 or ≥ 3.11.4 locally.
- **Not supported on a remote engine:** plan and thread turns, `workspace='in_place'`, and folders that are not Git projects.
- **Windows:** not tested; the source does go through the platform layer.
- The change is large (two commits, about 1 MB). Maintainers may ask to split it into "remote engines core" and "Remote switch UI".
