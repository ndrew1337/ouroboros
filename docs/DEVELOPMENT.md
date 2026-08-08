# DEVELOPMENT.md — Development Principles & Module Guide

## What This File Is

This is Ouroboros's **engineering handbook** — the bridge between philosophy (BIBLE.md) and architecture (ARCHITECTURE.md).

**BIBLE.md** answers *why* and *what matters*.
**ARCHITECTURE.md** describes *what exists right now*.
**DEVELOPMENT.md** answers *how to build* — the concrete principles, patterns, and checklists for writing, modifying, and reviewing code in this project.

## Scope

- **Code style & structure:** naming, file layout, module boundaries, error handling patterns.
- **Module lifecycle:** how to create a new module, what it must include, how it integrates.
- **Review & commit protocol:** what happens before code lands — gates, checks, invariants.
- **Testing standards:** what gets tested, how, minimum expectations.
- **Prompt engineering:** standards for writing and modifying LLM prompts (SYSTEM.md, CONSCIOUSNESS.md, etc.).
- **Integration patterns:** how modules communicate, data flows, shared state.

## What It Is NOT

- Not philosophy — that's BIBLE.md.
- Not an architecture map — that's ARCHITECTURE.md.
- Not a changelog — that's README.md + git log.
- Not aspirational — every rule here must reflect current practice or an immediately enforced standard.

## Relationship to Other Documents

```
BIBLE.md (soul — principles, constraints, identity)
    ↓ informs
DEVELOPMENT.md (hands — how to build, concretely)
    ↓ produces
ARCHITECTURE.md (mirror — what currently exists)
```

Rules in this file must not contradict BIBLE.md.

---

## Naming Convention

### General Rules

- **Language:** All code identifiers, comments, docstrings, and commit messages are in English.
- **Style:** Python PEP 8. Modules and variables — `snake_case`. Classes — `PascalCase`. Constants — `UPPER_SNAKE_CASE`.
- **Self-explanatory names** over abbreviations. A name should tell you what the thing *does*, not just what it *is*. Derived from P6 (Authenticity & Reality Discipline).

### Entity Types

| Entity Type | Purpose | Naming Pattern | Contains Business Logic? | Example |
|-------------|---------|----------------|--------------------------|---------|
| **Gateway** | Thin adapter to an external API. Wraps third-party SDK/HTTP calls into clean Python functions. | `{Platform}Gateway` | No. Pure I/O — translate calls in, translate responses out. | `BrowserGateway` |
| **Service** | Orchestrates a domain concern. May use one or more Gateways, manage state, apply business rules. | `{Domain}Service` | Yes. Coordinates, decides, transforms. | — |
| **Tool** | An LLM-callable function exposed to the agent. Thin wrapper that connects the agent to a Gateway or Service. | `{verb}_{noun}` (snake_case function) | Minimal. Validates input, calls Gateway/Service, formats output. | `read_file`, `browse_page`, `web_search` |

### Gateway Rules (recommended pattern, not enforced)

When adding a new external API integration, the recommended pattern is a **Gateway** class that isolates transport from business logic. The `ouroboros/gateways/` directory houses external API adapters. As the codebase grows, extract Gateways as needed.

When a Gateway exists, it should follow these guidelines:
- No business logic: no routing, no decisions. Just transport.
- Input/output: takes Python primitives, returns Python primitives.
- Error handling: translates platform-specific errors into consistent return values.
- Stateless where possible.

**Existing Gateways:**
- `ouroboros/gateways/claude_code.py` — Claude Agent SDK gateway. One live path since
  D10 retired the `claude_code_edit` tool: `run_readonly` (the api-route advisory review,
  no mutating tools; `CLAUDE_CODE_MODEL` stays a backend setting for it). It pins the
  delegated trust surface — no settings/MCP config loaded from the target directory —
  and enforces one derived tool allowlist plus read path confinement through PreToolUse
  hooks. Structured `ClaudeCodeResult` output.
- `ouroboros/gateways/claudexor.py` — Claudexor v3 control-plane gateway. Loopback
  discovery, protocol-major + minimum-version handshake, project registration, run
  start/poll/cancel. Typed refusals (`ClaudexorUnavailable`,
  `ClaudexorSubscriptionWindowExhausted`) instead of raw HTTP errors. Also reads the run
  tree Claudexor writes on disk (`attempt_containment`): the attempt's harness HOME is
  recorded only as an artifact, so a caller that must verify it has nowhere else to look
  (the OS boundary is also on the run detail as `candidates[].confinement`, but the
  artifact answers both halves per attempt), and the path layout is Claudexor's. The
  boundary mechanism is read as an OPAQUE string: which
  ones exist, and on which hosts, is the engine's business. `engine_at_least` is the one version-floor
  predicate, shared by the handshake and by every lane-specific floor. The daemon bearer
  token grants the whole `/v2` surface and must stay inside this module: never in a
  `ToolContext`, a child's environment, or a harness sandbox. Policy (which route, when
  to fall back, when to block) lives in `subagents.resolve_subagent_executor` and
  `tools/delegate.py`, never here. `start_run` accepts the caller's `idempotency_key`
  (defaulting to a random one) because only the caller knows what its LOGICAL start is;
  a random key per POST turns a lost response into a second live run. Run LIFECYCLE —
  who owns a run, whether a cancel was verified, whether a settlement landed — is
  `ouroboros/delegate_custody.py`, not transport.

`ouroboros/claudexor_runtime.py` owns engine delivery, separately from both the
gateway and `data/claudexor` auth/config/run state. Its tracked pin names one
public Claudexor closure by exact identity and the exact Node version proven
with it, including exact per-platform Node archive URL/size/SHA-256/member facts.
Build scripts fetch the closure before PyInstaller and package it as
`claudexor-runtime/<archive>`; managed updates of an older immutable app use the
same URL instead. A matching packaged Node is preferred. Source mode or a package
without that exact Node obtains the selected official archive under the same
foreground ensure lock, extracts only the named executable into `data/state/cx`,
and probes its version before the closure. Seed and download paths use exact
size/SHA-256 admission, private staging, identity probes, and atomic immutable
promotion. Do not add npm, `latest`/`next`, a mutable current pointer, a second
manifest verifier, or background update policy to this path. `status()` must
remain read-only.

`ouroboros/claudexor_daemon.py::ensure_owned_gateway` is the explicit lifecycle
seam used by Connect and actual delegated/reviewer starts. It may stage a new
target beside a live authenticated daemon, but never hot-swaps that process;
next spawn follows the code pin. A pinned target never silently degrades to a
PATH binary. The gateway below remains pure I/O.

### Relationship Between Entities

```
LLM Agent
    |  calls
Tool (read_file, web_search, browse_page)
    |  delegates to
Gateway or direct implementation
    |  calls
External API / filesystem / subprocess
```

Not every layer is required for every operation. Simple cases (e.g., `read_file`) go Tool → filesystem directly.

### CLI / Headless Additions

- CLI commands should stay thin: parse flags, call gateway HTTP/SSE endpoints,
  render stdout/stderr/JSONL, and avoid duplicating runtime business logic.
- Headless task features belong behind gateway task APIs and the existing
  supervisor queue. Do not add a second scheduler for benchmarks.
- External workspace support must keep Ouroboros governance context pinned to
  the system repo while contextual repo tools resolve against the active
  workspace through `ToolContext.active_repo_dir()`.
- Workspace-mode tasks must use an explicit allowlist, reject system-repo/data
  overlap, require a git worktree root, and return patch artifacts instead of
  committing in the target repository.
- Workspace parent/headless tasks may call `task_acceptance_review`. For roots
  in auto/required mode, this call only records evidence for the single
  host-owned acceptance panel; it makes no reviewer-model call and returns no
  authoritative verdict. They must not gain repo commit/restart/runtime-control
  tools or local-readonly subagent review mutation tools.
- External workspace completion must be gated on explicit artifact finalization:
  `workspace.patch` is served through the task artifact endpoint, strict patch
  CLI modes fail on missing/empty/failed artifacts, and `workspace_patch.json`
  records diagnostics instead of silently treating patch-builder failures as
  empty diffs.
- Workspace preflight belongs in the headless task create flow as read-only
  facts: compact summary in task metadata/prompt, full
  `workspace_preflight.json` as an artifact. Do not dump full manifests into
  the prompt and do not add benchmark-specific instructions.
- Dependency installation guidance is workspace-scoped: project-local installs
  are allowed for external tasks, while system/global installs are pro-mode
  safety-reviewed attempts. `sudo` must be noninteractive (`sudo -n`).
- Treat workspace mode as routing plus guardrails, not an OS sandbox. If stronger
  isolation becomes required, add a real Docker/SSH/remote tool-execution layer
  instead of expanding shell-command heuristics.
- Do not add a CLI file-manager surface. Attachments, task artifacts, and logs
  are the allowed v1 file-adjacent surfaces.

### Cognitive Quality

- Do not lower model quality, reasoning effort, max-token budgets, or context
  breadth for core cognitive loops (especially Background Consciousness, review,
  and self-evolution) as an incidental cost or latency optimization.
- If a change intentionally narrows cognitive horizon, make that owner decision
  explicit in the plan, docs, tests, and review packet. Silent quality downgrades
  are continuity regressions, not refactors.

### Anti-pattern: tool-choice / discoverability gaps via SYSTEM.md prose (v6.37.0)

Do NOT fix a tool-choice or affordance-discoverability failure (the model didn't
reach for the right tool) by accreting per-case instructions in `prompts/SYSTEM.md`.
If the tool's description is already correct and the model still misses it, the fix
is one of:
1. a better tool DESCRIPTION at the schema source (tool schemas are always loaded
   into context, so this reaches the model without prompt growth), or
2. a STRUCTURAL affordance that makes the intended action available at the point of
   need (e.g. an in-task tool, a typed contract field).

Growing SYSTEM.md one bullet per incident is a P2 patch-smell — it trains around a
single failure instead of removing the class, bloats the resident prompt, and
fragments behavior away from the SSOT (P7). Pattern instance: the cyber-racing task
ran `mkdir ~/Desktop` instead of creating an Ouroboros project even though
`promote_chat_to_task(project_name=…)` already described exactly that — the fix was a
structural `ensure_project_scope` in-task affordance, not a new SYSTEM.md rule.

### Anti-pattern: hand-maintained model pricing and admission-by-price

Do not add manually maintained model-price tables, prefix-inherited tariffs, or
numeric fallback prices. They go stale independently of provider billing while
continuing to look authoritative. A price table must never become a model allowlist
or dispatch gate: query the exact provider route's catalog with an exact model match
when an automatic source exists, prefer provider-reported settled cost, and otherwise
preserve `cost=None` / `cost_final=false`. Missing pricing is disclosed uncertainty,
not evidence that a model is free and not a reason to block a new model. Finite budget
rails still block when already-known accounted spend is exhausted or a known
reservation would exceed the remaining limit.

### Anti-pattern: content-derived identity for host-minted records (v6.73.0)

If the host itself created a record — a chat message, a task, a binding — its
identity is CAPTURED AT INGRESS and passed downstream BY VALUE as a typed
reference (e.g. `origin_message_ref = {chat_id, client_message_id, ts,
text_sha256}` built where `log_chat("in", …)` writes the canonical row). Do NOT
re-derive that identity later by searching logs/state for a row whose text
hash/equality/prefix matches whatever text the caller happens to hold: in an
LLM-first system the text is routinely rewritten between ingress and use, so
content-derived lookup fails exactly on the normal path (the four
start-message-loss incidents fixed serially before v6.73.0 were all this class).
Content hashes remain legitimate in two roles only: (a) an INTEGRITY CHECK on an
already-known identity (`text_sha256` inside a ref verifies the row wasn't
swapped — it is never the lookup key), and (b) content-ADDRESSING where the
content IS the identity (artifact stores, observability blobs, join-ledger
result hashes, staged-diff review bindings). One NAMED exception inside role (b),
v6.78.0 / owner Q28=B: a verification RECEIPT is reconciled by ONE TYPED
IDENTITY KEY — its `criterion_id` when it has one, else its canonical `check` text, else
(for the artifact-observation class, which runs no command) its observed `paths` SET —
because with no criterion id the check text / observed path set IS that verification's
identity (there is no earlier ingress point to capture, and a class whose identity is
dropped can never reconcile itself: a red "report.md missing" must be clearable by the
byte-identical green observation once the file exists). Two receipts name the same
verification when the key's KIND and VALUE both match, never across kinds. The key
replaced a per-component FALLBACK CHAIN, and the reason is worth stating as a rule: a
chain is not an equivalence relation. It was not transitive — `{c1, check}` matched
`{check}`, `{check}` matched `{c2, check}`, while `c1` and `c2` are explicitly different
criteria — so one check-only green reconciled two distinct reds, "collapse the candidates
onto the identity they name" was not even well defined, and the outstanding set came out
order-dependent. No care at the call sites can repair a relation that is not an
equivalence; keying makes sameness the KERNEL of a function (reflexive, symmetric,
transitive by construction) and makes an existing `criterion_id` authoritative
STRUCTURALLY rather than by a rule someone must remember. It is a SIMPLIFICATION —
strictly fewer branches — and it fails in the SAFE direction: strictly fewer
reconciliations, so a red the chain used to clear may now stay open. Concretely, a re-run
that OMITS the `criterion_id` it carried before no longer clears its own red; the cost is
one advisory nudge and one advisory reviewer flag, never a false green. Read that as the
design, not a regression. If omission tolerance is ever genuinely needed, the only sound
route is to carry the id forward STRUCTURALLY at receipt ingress (`tools/verify.py`) —
never to infer it back from shared command text under another name. Two paths deliberately
keep the older any-later-grounding rule, because there the key cannot do its job: a
receipt with NO key at all (nothing to protect — a malformed `artifact_observation` with
no paths would otherwise mint an unclearable red), and the MASKED-pass path, where the
only text identity is the masked command itself and the prescribed remediation ("drop the
masking pipe") necessarily changes it, so a byte-identical clean re-run cannot exist.
On that masked path the `criterion_id` key alone binds, by the same equality: a masked
receipt that NAMES a criterion is cleared only by a later clean receipt naming that SAME
criterion — one that merely omits its id does NOT clear it — and the any-later-clean
fallback reaches only a masked receipt that names no criterion. Command text never
participates there, in either direction. **And whatever decides must be what is
reported.** Both relations and both disclosures read ONE mode-aware projection —
`receipt_reconciliation_key(receipt, masked=…)`, with the mode read off the receipt by
`receipt_is_masked_pass` for the per-row question (`receipt_disclosed_reconciliation_key`)
— so `reconciliation_identity` and `expected_whitespace_normalized` name the authority
that actually cleared the receipt instead of re-deriving one beside it. Re-deriving is how
round 6 arrived: an id-less masked pass, reconciled by ANY later clean grounding, was
disclosed to the acceptance reviewer as `check`-governed with
`expected_whitespace_normalized=true`, while `_reconciles_masked` never looked at check
text at all. That flag is now FALSE across the whole masked path. A host-attested artifact
that misstates its own basis is worse than one that says nothing: the reviewer cannot
discount evidence whose provenance it has been told wrongly, and "the reporting path reads
the deciding path" is the general fix — the same move that collapsed the projection/
comparison split in round 4 and the fallback chain in round 5.

Round 7 was that same class one kind over: `expected_whitespace_normalized` also read true
for `artifact_paths`, whose observed set is compared BYTE-FOR-BYTE (every whitespace byte of
a filename counts), so the fix for one kind had simply never been asked of the others. The
durable form is to answer the question ONCE PER KIND, next to the kinds: `IDENTITY_KINDS` is
the closed, ordered table of reconciliation identity kinds, each row carrying its name, how
to read its value off a `ReceiptIdentity`, and whether that identity is canonical command
text; `ReceiptIdentity.key` iterates the table and `KIND_NORMALIZES_COMMAND_TEXT` is the
total lookup the flag performs — true for `check`, false for `criterion_id`,
`artifact_paths` and `none`, with a `KeyError` rather than a default for a kind that skipped
the table. The general rule: when a disclosure describes a property of a closed set of
kinds, put the property IN the set, so a new kind cannot be added without answering, and a
per-kind fix cannot be mistaken for a fix of the class. Every component that
participates carries into BOTH fixed reviewer projections
(`verification_receipt_ledger_row`, `_accept_verification_summary`), and it does so
through ONE shared renderer — `_outcome_receipts.receipt_identity_projection` — rather
than two independently maintained key lists, so the two surfaces cannot drift apart
about the identity the reconciliation used. Three rules make that claim literal rather
than aspirational. First, a projection that BOUNDS a list discloses the bounding, and it
does so through ONE shared helper — `_outcome_receipts.disclosed_list_projection` — not a
hand-rolled `[:N]` per call site: every bounded list on a cognitive-review surface emits
`<key>_omitted` (the exact count, 0 included), bounds each string through the SSOT
`utils.truncate_review_artifact` so a clipped value carries its own omission note, and
adds a hash of the FULL set (`paths_identity_sha256` for the path identity `_reconciles`
compares; `urls_identity_sha256` for the native-retrieval URL set) wherever the complete
evidence is not reachable from the store the bounded row lives in — the receipt lists need
no hash because the whole receipt is durable in the per-task `verification_receipts.jsonl`.
Bounding a set is allowed; hiding that you bounded it is the P1 violation, and a review
round that finds one surviving `[:N]` on such a surface should sweep the phase for the
rest rather than patch the instance. Second, "is anything still outstanding?" is a
question about a SET of identities, so it is answered by an outstanding SET
(`unreconciled_failed` / `unreconciled_masked`, each candidate scanned against ALL later
reconcilers) and never by a single latest-POINTER, which a newer candidate silently
overwrites: fail A, fail B, pass B once reported no red at all, and masked c1, masked c2,
clean c2 lost c1 the same way. The `latest_*` helpers are projections of that set, and the
acceptance summary carries its SIZE (`unreconciled_red_count`,
`check_exit_masking_unreconciled_count`) so a second outstanding item cannot hide behind a
flag that reads as if it described exactly one. Third, the acceptance summary projects the
identity of the UNRECONCILED RED (`unreconciled_red_identity`) and not only of the latest
receipt: a later green of a DIFFERENT verification leaves an earlier red standing, so a
reviewer shown `unreconciled_red=true` beside a green `latest_*` would see a flag whose
cause is nowhere in the packet. A flag without its cause is not reconstructible. Fourth —
the rule the first three kept re-learning instance by instance — there is exactly ONE
canonical identity derivation (`_outcome_receipts.receipt_canonical_identity`, built on
the shared `shell_parse.canonical_command_text` seam and `canonical_path_set`), and
comparison, hashing, counting and projection all read THAT object. A phase that carries a
normalized/set-shaped identity for comparison and a raw/ordered one for display will keep
producing findings where the two disagree: a lossy comparison form (`" ".join(x.split())`
collapses whitespace inside quoted arguments, so two checks asserting different things
compare equal and a green closes an unrelated red), rows counted where identities were
promised, and a hash describing a set the carried items do not. The derivation implies an
ORDER, and the order is always canonicalize the RAW values → render → bound: rendering
is lossy (redaction, truncation), so de-duplicating after it drops distinct values while
the omitted count still reports zero.

Two consequences of that fourth rule are worth writing down rather than rediscovering.
**The receipt store's `check` rendering changed in v6.78.0**: `tools/verify.py` now writes
`shlex.join(argv)` where it wrote `" ".join(argv)`. A space-join is not injective — argv
`["echo","a b"]` and `["echo","a","b"]` rendered to the same text, and since that text IS
the verification's identity downstream, a green on one could clear a red on the other.
`shlex.join` is the exact inverse of the lexer `canonical_command_text` re-tokenizes with,
so the stored text round-trips back to the argv that ran — which only holds because that
lexer now preserves QUOTING: `shlex` strips quotes before yielding tokens, so `echo '&&' x`
and `echo && x` arrived identical, and the canonicalizer's leading/trailing separator strip
then dropped a literal final `&&` argument as if it were syntax. Quoted and escaped
punctuation is marked on the way into the lexer and the mark is read back off the tokens,
so a literal argument that merely spells like an operator can never be mistaken for one;
nothing is stripped afterwards. Path values are likewise left byte-for-byte alone — a
leading or trailing space is a legal filename byte, and trimming it let observations of two
different files reconcile. The rule behind all three: a normalization that discards
information the identity depends on is not a normalization.

**Changing a stored rendering means versioning it.** The cross-version cost of that switch
was first written down here as safe in ONE direction — an old receipt carries `echo a b`
where a new one for the same argv carries `echo 'a b'`, so they fail to reconcile, and a
non-reconciling red STAYS RED. True, and not the whole picture: an old red and a new green
from DIFFERENT argvs can render IDENTICALLY (`["echo","a b"]` and `["echo","a","b"]` were
both space-joined to `echo a b`), and there the new green cleared the old red — a false
green produced by the change made to remove one. Reasoning about a format migration one
direction at a time is how that got missed; ask both, always. The root was that the receipt
did not record WHICH renderer produced its text, so the comparator could not tell the two
formats apart. It records it now: `check_rendering` is stamped beside the text by every
writer that renders one (`shlex_join` for a rendered argv, `declared_text` for the agent's
own verbatim text), an absent stamp reads `unversioned`, and the check identity is the
RENDERING PAIRED WITH THE TEXT. Receipts from different renderings are therefore never the
same verification — an `unversioned` receipt is not known-equal to a versioned one, it is
UNKNOWN, and unknown must not clear a red. Two `unversioned` receipts still match each
other, which is both the behaviour they had before the upgrade and the most that can be
recovered from strings already on disk. An unrecognised future rendering is automatically
its own namespace, so the next renderer change is safe without a code change — but it must
still take a new stamp value, and a writer that stores a `check` without one is a bug (a
test walks `verify.py`'s receipt writers and asserts every check-writing one stamps).
The direction is now honestly stated: cross-version reconciliation is strictly LESS likely,
so an upgrade may leave standing a red that was really fixed. A false red costs a human a
second look; a false green costs the thing this whole surface exists for. The other two
identities were asked the same question and neither has the hole: the MASKED path keys on
the agent-authored `criterion_id`, which no writer ever re-rendered, and the observed
`paths` set is stored RAW and canonicalized by the READER, so both eras are compared on
today's terms — the phase's path change (dropping `.strip()`) was a comparator change, not
a stored-format one. **And one KNOWN, deliberately
deferred limit:** `tools/verify.py` bounds an `artifact_observation` receipt's observed
path set at twenty (`paths[:20]`) with no omission count, so two observations differing
only past the twentieth path are indistinguishable IN THE STORE. Unlike every bound above
it, this one is on what gets written DURABLY, not on what gets projected — there is no
complete set behind it for a downstream projection to recover, and `paths_identity_sha256`
can only ever hash what the writer wrote. Fixing it means changing what the durable store
holds and deserves its own scope. It is recorded here so it is a known limit rather than a
silent one. It is ADVISORY only — it shapes a nudge and a
disclosed reviewer flag (`expected_whitespace_normalized`), never a gate — so a
mismatch costs at most one advisory nudge and can never lose a record. For semantic matching of fuzzy
entities, use the LLM-first pattern (`semantic_dedup`: exact fingerprint as a
cheap first pass, an LLM as the authority, fail-open) — never string equality.
The enforcement shape is a REQUIRED typed argument at the consuming seam
(`bind_task_to_project(..., *, origin)`: a valid ref or a closed-enum absence
reason; omission raises), so a future call site cannot silently skip the
invariant.

### Mutable external-fact inventory

This table is a maintenance inventory, not a second runtime authority. External
facts change independently of Ouroboros releases; prefer live metadata or a
bounded probe where that can answer the exact question, and otherwise keep the
current conservative behavior visible. v6.67.0 documents these facts but does
not migrate their runtime representations.

| Location | Fact | Mutability | Current authority | Live/probe option | Risk | Recommendation |
|----------|------|------------|-------------------|-------------------|------|----------------|
| `ouroboros/provider_models.py::_VISION_MODEL_PREFIXES` / `_VISION_OVERLAY` | Which model families accept native image input | High as model families and route capabilities change | Conservative shipped prefixes, overridden by parsed OpenRouter `/models` `architecture.input_modalities` for exact model ids | Exact provider metadata when available; otherwise a bounded image-input capability probe | A stale positive sends unsupported image blocks; a stale negative needlessly captions them | Keep the conservative fallback and exact-model overlay; consider broader provider metadata only in a separately reviewed migration |
| `ouroboros/llm.py::supports_message_cache_control` and `_reasoning_signature_portable_across_or_providers` | Which families support message cache controls and portable replayed reasoning | Medium/high as provider routing contracts change | Explicit family rules backed by provider behavior and dated live probes | Provider documentation plus a same-model cross-provider replay probe | A false positive can invalidate a request; a false negative loses cache or reasoning continuity | Retain the small explicit rules and re-probe when provider behavior changes; do not generalize by model-name resemblance |
| `ouroboros/provider_models.py::_ANTHROPIC_MODEL_ALIASES` / `migrate_model_value` | Direct-provider id spelling compatibility | Medium as providers rename ids and prefixes | Shipped compatibility mapping and current direct-provider id contract | Exact provider catalog/documentation can confirm a current id, but cannot establish whether a saved spelling was intentional | Removing an alias breaks upgrades; guessing aliases can silently reroute | Keep explicit compatibility aliases until a separately documented retirement window closes |
| `ouroboros/server_runtime.py::_RETIRED_MODEL_DEFAULT_REPLACEMENTS` and scope prior/legacy defaults | Which formerly shipped defaults are upgraded automatically | Release-dependent | Release history plus current `SETTINGS_DEFAULTS`; only known former defaults are migrated | A live catalog can show availability, but cannot infer user intent or whether a saved value was a default | Over-broad migration overwrites an explicit owner choice | Keep release-scoped exact replacements and regression tests; review retirement separately |
| `ouroboros/pricing.py::get_pricing` and `ouroboros/llm.py::fetch_openrouter_pricing` / `fetch_cloudru_pricing` | Exact-route model tariffs | High; pricing and FX drift independently | Exact provider catalog with nullable unknowns; provider-settled usage wins | Bounded live catalog fetch and provider-reported settled cost | Static prices look authoritative after becoming wrong and can corrupt admission | Preserve the live nullable design and cover it by regression; do not restore runtime tariff tables |

### Provider Independence

Ouroboros must remain fully operational when configured with a SINGLE isolated
provider — a local model, or only one of OpenAI / Anthropic / MiniMax / Cloud.ru /
GigaChat — with no second provider and no OpenRouter. This is a standing invariant, not a
per-feature nicety:

- **Core capability floor.** The agent loop, the multi-model commit (triad)
  review, the scope review, and the memory/context flows must all work on the
  single configured provider. A change that makes any of these silently require a
  second provider (or OpenRouter specifically) is a regression, not a feature.
- **Slot self-sufficiency.** Each exclusive direct provider auto-fills every model
  slot AND the review/scope reviewer slots from its own prefixed models
  (`server_runtime.apply_runtime_provider_defaults` + the `*_DIRECT_DEFAULTS` maps
  in `provider_models.py`). When adding a provider, wire its defaults, credential
  detection (`_exclusive_direct_remote_provider(_env)`, `has_remote_provider`),
  safety light-model reachability, automatic route pricing when the provider exposes
  it (otherwise honest unknown cost), model-catalog listing, AND
  the `config.py` env-time review/scope fallback allow-list
  (`direct_provider_review_models_fallback`, consumed by `get_review_models` /
  `get_scope_review_models`) so no slot — model OR reviewer — is left pointing at
  an unconfigured provider.
  EXCEPTION (v6.82.0): the deep-self-review slot is filled only for providers whose
  model carries the >=1M window that review sizes against — OpenAI and Anthropic.
  Cloud.ru and GigaChat are documented below that floor, and MiniMax guarantees only
  a 512K minimum ("up to 1M"), so their shipped deep value is CLEARED instead (an
  explicit owner value is never touched) and deep review is honestly unavailable
  rather than advertised and doomed to overflow its route.
- **Scope-review ≥1M floor (BIBLE P3).** A direct-provider-only setup fills the
  scope-reviewer slot with its own model (mirroring the Cloud.ru pattern). Where the
  single provider has no 1M-context model, the disclosed fallback (v6.80.0) is the
  owner-selected `low` context mode: whole-repository scope review is then declaredly
  not performed and every commit records a typed `skipped_low_context_mode` evidence
  row (the deprecated `OUROBOROS_SCOPE_REVIEW_FLOOR` is NOT that fallback — writing it
  changes nothing). The ≥1M floor is never lowered as a code default, the removed
  `OUROBOROS_SCOPE_REVIEW_DEGRADED` partial-coverage reviewer is NOT replaced by
  anything that looks like the gate, and the blocking triad still reviews the staged
  diff in full in both modes. Since v6.55.0 the no-evidence 1M sentinel keys on
  the shipped default reviewer (openai/gpt-5.6-terra as of v6.82.0; 1.05M window per
  OpenRouter /models metadata, checked 2026-07-29), so an install pinning any OTHER
  reviewer runs in the visible sub-floor window until Capability Evidence lands
  (generative probe or `/api/owner/capability-ack`); an OpenAI-only install's
  designated scope reviewer is now the same terra model (direct spelling), so it keeps
  the sentinel; the blocking triad is unaffected. Since v6.87.44 that sentinel is
  SIZING-only for EVERY route, the shipped default included: the designated default
  acquires no blocking authority from its name — its route is probed like any pin
  (metadata-only, rate-limited by the evidence TTL, v6.87.45) and it may BLOCK only
  on sourced, non-stale ≥1M evidence (`ReviewerWindow.blocking_authority_allowed`).
  The sentinel sizes the prompt; the evidence signs the verdict. On a connected
  install the automatic first-use metadata probe sources that evidence immediately,
  so the distinction is visible mainly when the provider cannot be reached. That ack is
  REACHABLE from the UI: a scope-slot change makes the settings save probe the slot's
  own route and return `review_capability_notices` carrying the same
  `needs_ack:{route, route_fp, evidence}` payload the Max gate uses, which
  `settings.js` renders through the same confirm flow. And an install that was
  AUTO-downgraded to `low` can declare that `low` by re-selecting it in either owner
  control (`/api/state` exposes `context_mode_auto_low` so the no-op click is not
  short-circuited), which is what makes the fallback above reachable at all.
- **Local-only installs keep the local route.** Light and Fallback ship real remote
  defaults since v6.82.0, so `server_runtime._clear_shipped_defaults_for_local_only`
  blanks an UNTOUCHED shipped default in those slots when no remote credential is
  configured. The two outcomes DIFFER and both are intended: an empty Light slot
  inherits Main, which is the local route, while an empty Fallbacks slot disables the
  cross-model fallback chain (`config.parse_fallback_chain`) rather than inheriting
  anything — an unreachable chain is worse than none. A slot the owner explicitly
  routed to local (`USE_LOCAL_LIGHT` / `USE_LOCAL_FALLBACK`) is REACHABLE and is never
  cleared, and an explicit model choice is never cleared. Lane inheritance keys off
  ENV PRESENCE, not string equality, so an empty Light slot follows Main even when
  Main happens to equal Light's shipped default. When adding a slot default, ask
  whether a local-only install can still reach it; if not, it belongs in that guard —
  together with the PRIOR shipped value, because an upgraded local-only file still
  carries it.
  Review and scope-review slots follow the same rule at call time: a local-only
  install normalizes each configured slot to local Main and carries explicit local
  routing through `ReviewSlot`; Max-mode scope review may still fail honestly when
  the local context is below the 1M floor, while owner-selected Low records the
  established typed scope skip instead of contacting an unconfigured provider.
- **Direct-OpenAI deep review runs plain Sol.** The shipped OpenRouter default is the slug `openai/gpt-5.6-sol-pro`; that `-pro` is an OpenRouter routing slug, not an OpenAI model id (live-probed 2026-07-29: 404 on `/v1/chat/completions`). OpenAI exposes pro reasoning only as `reasoning.mode="pro"` on `/v1/responses`, and `/v1/chat/completions` rejects a `reasoning` parameter (400). Every LLM call in `llm.py` is a chat.completions call, so `OPENAI_DIRECT_DEFAULTS` ships `openai::gpt-5.6-sol` — an owner-accepted capability difference, not an oversight. Adding a Responses-API lane is the follow-up that would close it; until then never put a `-pro` id in a direct slot.
- **Anthropic-only disclosures.** The auto-filled review triad for an Anthropic-only direct install is the loud single-model [sonnet-5]×3 (main==light — deliberate), and the auto scope slot runs in the visible sub-floor window until Capability Evidence or an owner acknowledgement lands, because the designated-default sentinel moved off the Anthropic family with the v6.82.0 defaults.
- **Documented exceptions.** A few provider-specific extras are deliberately NOT
  universal: `web_search` (OpenAI Responses, OpenRouter server tool, Anthropic
  server tool, optional ddgs) and the Claude Agent SDK tools (Anthropic). These
  must degrade gracefully — be unavailable and clearly surfaced under a
  non-matching single provider, never crash the core loop. Do not expand this
  exception list silently.

---

## Module Size & Complexity

Derived from P7 (Minimalism): entire codebase fits in one context window.

- Module target: ~1000 lines. Crossing that line is P7 pressure and should trigger extraction or an explicit justification.
- Module hard gate: 1600 lines for non-grandfathered modules in `tests/test_smoke.py`. Grandfathered (`GRANDFATHERED_OVERSIZED_MODULES` in `ouroboros/review.py`): `llm.py`, `claude_advisory_review.py`, `review_state.py`, `server.py`, temporary v5.7.1 debt `git.py`, and temporary v6.15/v6.16 debt `extension_loader.py` (OOP extension parity plus worker->server companion reconcile crossed the gate; the registry-coupled `PluginAPIImpl`/loader split is the deferred follow-up), and v6.20.0 acting-subagents debt `registry.py` / `events.py` (the acting authority/gating grew the tool dispatcher and the supervisor schedule handler past the 1600 gate; extracting their safety-critical dispatch/event internals is the deferred follow-up), v6.33.0 reliability debt `loop.py` / `shell.py` / `core.py` (deadline-aware finalization, the brace-group `sh -c` hint, single-file search, and the re-read-awareness nudge crossed three hot tool/loop modules whose helpers are tightly coupled to internals — a clean split fights the function-size gate and risks import cycles, so it is tracked debt), and v6.50.0 reconciliation-layer debt `control.py` / `workers.py` (typed schedule admission, cap serialization, and parent-side advisory reconciliation grew the existing scheduling surfaces; splitting before the new contract stabilizes would add indirection around the critical path), and v6.63.0 skill-payload debt `skills/unix_computer_use/plugin.py` (the OSWorld remote osworld_http/ssh_macos backends — connection registry, remote screenshot/input/exec translation, fail-closed guards — grew the skill past the gate; extracting the remote translation layer into a sibling payload module is the deferred follow-up; this entry is repo-path-qualified so a future skill's `plugin.py` is not silently exempted) — split deferred until each surface stabilises. The authoritative grandfathered set is `GRANDFATHERED_OVERSIZED_MODULES` in `ouroboros/review.py`.
- Method target: <150 lines. Crossing that line is a decomposition signal, not an automatic failure by itself.
- Method hard gate: 300 lines in `tests/test_smoke.py`.
- Runtime-code function-count hard gate: enforced by `tests/test_smoke.py` against the value defined in `ouroboros/review.py::MAX_TOTAL_FUNCTIONS` (single source of truth — bump the constant when adding a feature with an explicit comment justifying the increase). Tracked `devtools/` operator code is excluded from this runtime health gate, but touched `devtools/` files are still fully reviewed. Precedent (2026-06-10, owner decision): the first consolidation paydown removed ~60 dead/duplicate/trivial-wrapper functions and the cap moved to 3500 with deliberate headroom — the gate exists to force acknowledged growth, not to sit at zero slack and churn on every small fix.
- Function parameters: <8.
- Net complexity growth per cycle approaches zero.
- If a feature is not used in the current cycle — it is premature.

### Pragmatic SOLID

SOLID is a direction for making changes legible to future agents, not a demand
for classes or extra framework surface:

- **SRP — Single Responsibility Principle:** keep one coherent reason and one
  clear authority for a unit to change.
- **OCP — Open/Closed Principle:** extend an existing stable seam when it
  preserves the contract instead of rewriting unrelated callers.
- **LSP — Liskov Substitution Principle:** an implementation or backend must
  preserve the caller-visible behavior of the contract it implements.
- **ISP — Interface Segregation Principle:** consumers should depend only on
  the capabilities they actually use, not a broad convenience interface.
- **DIP — Dependency Inversion Principle:** policy should depend on small,
  host-owned contracts rather than provider-specific or concrete details.

Apply these principles pragmatically. They do not require a class hierarchy,
DI container, numeric score, AST analyzer, or a new review pass. A SOLID or
minimalism finding must name the exact symbol or authority, the concrete
duplication or coupling, and a smaller alternative that still satisfies the
contract. Diff size, line count, and file count alone are not findings.

---

## Core Governance Artifacts

`BIBLE.md`, `docs/ARCHITECTURE.md`, and `docs/DEVELOPMENT.md` are **core governance artifacts**.
They are the constitutional, architectural, and procedural ground truth of the system.

### Invariant: Full availability in reasoning flows

Any flow that requires architectural, constitutional, or procedural reasoning MUST include
these artifacts as **first-class context sections** — not as optional or opportunistic
inclusions via touched-file packs.

Concrete requirements:

| Flow | BIBLE.md | ARCHITECTURE.md | DEVELOPMENT.md |
|------|----------|-----------------|----------------|
| Main task context (`context.py`) | ✅ full (tier-0) | ✅ full for self-body tasks in max; navigation map for low, for external/headless/workspace tasks, and (v6.30.0) for evolution cycles — unless self-body docs are explicitly required (task field or contract) | ✅ full for self-body/runnable repo tasks (incl. evolution); external/headless/workspace tasks get an on-demand pointer unless the contract explicitly requires self-body docs |
| Triad review (`tools/review.py`) | ✅ via preamble | ✅ via `load_governance_doc` | ✅ via `load_governance_doc` |
| ↳ Anti-thrashing (v4.35.1) | — | — | Open obligations loaded from `review_state` via `load_state(drive_root)` + `make_repo_key(repo_dir)`, injected unconditionally into `_build_review_history_section` prompt context. Same mechanism in `scope_review.py::_build_scope_prompt` (best-effort when `drive_root` available). |
| Background consciousness (`consciousness.py`) | ✅ full | ✅ full (max) / navigation map (low) | — (not yet required) |
| Advisory pre-review (`tools/claude_advisory_review.py`) | ✅ via `load_governance_doc` | ✅ via `load_governance_doc` | ✅ via `load_governance_doc` |
| Scope review (`tools/scope_review.py`) | full canonical doc + Atlas accounting | full canonical doc + Atlas accounting | full canonical doc + Atlas accounting |
| Plan review (`tools/plan_review.py`) | ✅ full (every plan class) | full for `plan_class=self_mod`; lossless **navigation map** (sections + line ranges, full sections on demand) for external/creative/research plans (v6.61.0, owner-approved governance change) | ✅ full (every plan class) |
| Deep self-review (`deep_self_review.py`) | full canonical doc + Atlas accounting | full (max) / navigation map (low) + Atlas accounting | full canonical doc + Atlas accounting |

Plan review always keeps BIBLE.md, DEVELOPMENT.md, the proposed plan,
touched-file snapshots, and reviewer-slot framing as first-class context.
ARCHITECTURE.md is CLASS-TIERED (v6.61.0, an owner-approved governance
evolution — quiz 19): the agent declares `plan_class`
(`self_mod | external | creative | research`), and the host STRUCTURALLY
escalates to `self_mod` whenever `files_to_touch` resolve under the system repo
(a path fact, never keyword matching). `self_mod` plans keep the full inline
ARCHITECTURE.md — unchanged from the historical contract. Non-self_mod plans
(an external codebase, a creative deliverable, a research question) receive
ARCHITECTURE.md as the LOSSLESS navigation map (`context_layout.
generate_doc_nav_map`: every section + line range, full sections readable on
demand) — their reviewers judge the plan against its own domain, not ~45K
tokens of self-body detail. Rationale: the full-pack requirement existed to
protect SELF-modification reasoning; for non-self plans it actively hurt
review quality (reviewers anchored on runtime internals irrelevant to the
deliverable) while tripling cost. The agent must choose `context_level`
explicitly for `self_mod` plans; non-self_mod plans may omit it (defaults to
`minimal`). That field controls only the generated repository Atlas: `minimal`
omits Atlas accounting for bounded/local plans, while `localized`, `broad`, and
`constitutional` add progressively larger Atlas packs. A typed non-minimal
Atlas assembly failure or final quorum-fit failure rebuilds the same requested
fingerprint/scout wave once at loud `minimal`; compiler exceptions, monetary
budget refusal, and a minimal prompt that still cannot fit do not. Planning
scouts are likewise class-framed: `self_mod` scouts keep the repo-archaeology emphasis;
external/creative/research scouts are steered to the plan's own domain
(requirements, verification, sources, design) and never default to Ouroboros
internals.

Planning has two distinct roots. Governance documents are always loaded from
the system repository; planned snapshots and Atlas inventory always use
`active_repo_dir_for(ctx)`. A workspace/subject mismatch, an unavailable root,
or a `files_to_touch` path escaping that subject must fail loudly. Do not fall
back to reviewing the Ouroboros repo for an external plan. Read-only scouts use
the existing worker pool with its generic `executor=auto` route (selected
healthy harness first, existing loud native fallback) and persist full raw
handoffs. Wait for every launched
scout until it is terminal or the shared swarm ceiling is reached; give the
panel every ready non-empty handoff and an explicit reason for every omission.
Launch only one scout wave per exact plan fingerprint. A handoff is marked
consumed only after it was actually included in the reviewer request; a late
terminal handoff is audit-only and never reopens an already considered plan.
Canonical intent, task aliases, forensic refs, and omissions belong in one
shared evidence horizon—not copied corpora or a second planning engine.

The planning horizon must state the goal, mandatory invariants, scope
boundaries, non-goals, chosen existing extension seam, and explicitly rejected
expansions. Plan review publishes exactly `GREEN`, `REVIEW_REQUIRED`, or
`REVISE_PLAN`. `REVIEW_REQUIRED` findings are inputs: the main agent may accept,
reject, or defer any/all of them. Blocking closes the latest still-current,
reviewed, integrated, non-degraded result without a second LLM call through a
separate `plan_task` call containing `review_disposition` only: every finding
appears exactly once with evidence-based rationale, and each acceptance names
the matching plan revision. Never replay plan/goal/scope/files/context with the
disposition. Mixed calls and vacuous disposition-only calls fail before a new
attempt is recorded; exact replay is idempotent. Blocking `REVISE_PLAN` requires
changed plan text/fingerprint and another panel, while advisory may proceed only
under loud host disclosure and the main agent's rationale. Unknown, stale,
duplicate, contradictory, or incomplete dispositions fail closed. Reviewers
remain generative, but a finding must name
a concrete defect or a concrete smaller existing extension seam; never require
a fixed number of findings.

Force-plan is an LLM-first pre-implementation obligation on the admitted managed
root, not a mechanical permission check around implementation tools. The existing
`plan_review_state` owns durable review authority and
`config.get_review_enforcement()` owns blocking/advisory policy. Every submitted
envelope that reaches `plan_task` supersedes prior authority: invalid plan/goal/scope
input stores a domain-separated open attempt, while a valid envelope stores its
canonical fingerprint before repository/path validation. A newer attempt therefore
cannot fall back to an older GREEN. Immediately before first panel dispatch, the exact planning-scout
handoff component is frozen in a fingerprint-keyed host write-once continuity artifact;
the remaining live reviewer context is rebuilt. An unavailable reviewer
never becomes a disposition-able verdict; a repeat call reuses that handoff snapshot and
retries the panel, including after A→B→A. Blocking stays in
analysis and non-mutating preparation until closure or a real task-wide rail;
advisory may proceed by agent judgment with a host-owned disclosure, including
an explicitly rejected `REVISE_PLAN`. A planning
deadline skip records a typed rail attempt before returning so the reducer cannot
misread it as an absent `plan_task` call.
The short-lived Swarm router admits one new root and transfers the intent; it
never runs `plan_task`, steers an existing task, or publishes the work inline.

**Context mode (low / max).** The owner-selected `OUROBOROS_CONTEXT_MODE`
(layout SSOT: `ouroboros/context_layout.py`) tiers the *reference-doc* layer of
the agent's own context (main task context, background consciousness, deep
self-review). In `max`, self-body tasks inline ARCHITECTURE.md and DEVELOPMENT.md
in full. External/headless/workspace tasks receive ARCHITECTURE.md as a lossless
navigation map and DEVELOPMENT.md as an on-demand pointer unless their
`task_contract` explicitly requires self-body docs. In `low` (for ~200K / local
models), ARCHITECTURE.md is a lossless **navigation map** (every section + line
range; full sections read on demand via `read_file`), and DEVELOPMENT.md stays
full for runnable repo/self-body task contexts unless a structured caller
explicitly sets `context_requires_development=false` (then a visible on-demand
pointer is used). README.md and CHECKLISTS.md are not inlined in the agent
context in either mode (README is user-facing; reviewers load their own
CHECKLISTS copy). The tier-0 protected core — SYSTEM.md, BIBLE.md,
identity, scratchpad, knowledge index, recent dialogue — is ALWAYS full in every
mode (BIBLE P1 cognitive-horizon / P4). Context mode is owner-only (the agent
cannot lower it) and never changes model / reasoning-effort / output budgets; the
blocking scope reviewer's ≥1M context floor (P3) is untouched.

For ordinary Main calls, `context_fit.py` may render Max and Low from one
immutable captured core and apply exact family+route calibration. Unknown
routes try Max; there is no silent 200K assumption. Only a confirmed physical
overflow may retry the same model once with a task-local Low projection, with
forensic and owner-visible disclosure. This never changes the global context
mode and never applies to P3 commit/scope review.

### Invariant: No silent truncation

If a core governance artifact cannot fit in the available context budget:
- Do **not** silently omit it or truncate it without a visible marker.
- Either adjust the budget/flow to accommodate it, or emit an explicit warning
  (`⚠️ OMISSION NOTE: ARCHITECTURE.md omitted due to budget constraints`) so the
  operator and the model both know the context is incomplete.
- A reviewer or agent operating without ARCHITECTURE.md MUST NOT be treated as
  operating with full context — findings may be incomplete.
- Tools that return multi-model review findings (`commit_reviewed`, `skill_review`,
  scope/advisory review helpers) MUST be listed in
  `UNTRUNCATED_TOOL_RESULTS` or have an explicit per-tool limit; the default
  15KB transport cap is not acceptable for review verdicts.
- A reference-doc **navigation map** (full sections one `read_file` away) and a
  named on-demand pointer are visible, lossless representations — NOT silent
  truncation. The low context mode uses these; it never applies `[:N]` to a doc.
- String bounding goes through the SSOT `utils.truncate_review_artifact`, never a
  hand-rolled `text[:cap] + marker`. Besides the marker, that helper carries an
  anti-waste FLOOR: a cut saving fewer characters than its own omission note is pure
  damage, so below it the text passes through whole. A local re-implementation loses
  the floor and can return a value LONGER than the input it "shortened" (a `…[+N
  chars]` marker is 11 characters, so any overflow under that grew the field).
- Bounding a LIST is subject to the same rule as bounding a string: a `[:N]` slice
  must be accompanied by an explicit omitted COUNT, and — where the slice touches an
  identity that something downstream compares — a durable hash or reference for the
  full set (see `_outcome_receipts.receipt_identity_projection`).

### Invariant: Owner-facing surfaces show the full text (v6.70.0)

Disclosed truncation (the `⚠️ OMISSION NOTE` marker) exists to protect **LLM
context budgets** — it is a model-bound mechanism, not a licence to shorten
what the owner reads:

- **Owner/UI-bound surfaces** (chat panels, task_results projections, review
  verdicts shown to a person) present the COMPLETE text, or carry a reference
  to a durable full copy (e.g. an observability `response_ref`). Reviewer
  rationale is a cognitive artifact (BIBLE P1): projecting it truncated while
  the full copy sits unreferenced in private blobs is partial memory loss.
- **Model-bound projections** (review packs, context sections, tool-result
  transport) keep their disclosed-truncation budgets — those are real context
  economics.
- **A cut cheaper than its own marker is forbidden everywhere.** Truncation
  that saves fewer characters than the omission note it appends is pure
  damage; the shared primitive (`utils.truncate_review_artifact`) enforces
  this floor, and new truncation sites must reuse it rather than hand-rolling
  `[:N]` + marker. One named exception: tiny single-line identifier fields
  (limit < 100, e.g. a reflection backlog `kind`) keep a plain hard slice —
  a multi-line omission marker inside a one-line value is worse damage than
  the cut it discloses.

### Invariant: No "only if touched" gate for core artifacts

Core governance artifacts reach review/reasoning flows unconditionally — NOT only
when they appear in `touched_paths`. The `build_touched_file_pack` function is for
_changed_ files; core artifacts are a separate concern and are loaded independently.

### When adding a new reasoning flow

If you add a new flow that reasons about code structure, system architecture, or
engineering standards, you MUST:
1. Explicitly load `ARCHITECTURE.md` (and BIBLE.md if constitutional reasoning applies).
2. Log a warning if the file is missing or unavailable — do not silently skip.
3. Add a test asserting the file is present in the assembled context/prompt.

---

## Review & Commit Protocol

Reviewed commits now have an explicit **two-step gate**:

1. **Advisory freshness gate**: finish all edits, then run `advisory_review`.
   Without a bypass, `commit_reviewed` requires a fresh matching
   advisory run, no open obligations from earlier blocked rounds, and no open
   commit-readiness debt. Any edit after advisory makes it stale and requires a
   re-run. When debt remains, `review_status` reports `repo_commit_ready=false`
   plus `retry_anchor=commit_readiness_debt` so the next retry starts from the
   repeated root cause rather than one obligation at a time. `skip_advisory_review=True`
   is an **absolute** escape hatch: it short-circuits the entire commit gate
   after writing an audit entry to `events.jsonl`. Open obligations and open
   commit-readiness debt stay visible in `review_status` (`repo_commit_ready`
   stays `false`) but do NOT block the bypassed commit. Use bypass when advisory
   cannot run (provider outage, rate limit) or when the stale signals are known
   to be obsolete; in both cases subsequent `on_successful_commit()` clears
   them automatically.
2. **Unified pre-commit review**: once advisory is fresh, the reviewed commit path
   runs reviewer slots in parallel on the exact staged snapshot. The durable
   review fingerprint binds `git write-tree`, ordered `HEAD`/`MERGE_HEAD` parents,
   indexed VERSION, expected `v{VERSION}` tag and any existing target, plus the
   binary staged-diff hash. The same binding is re-read after review and verified
   against the created commit/tag before push:
   - **Triad review** (`ouroboros/tools/review.py` + `ouroboros/triad_review.py`,
     orchestrated by `ouroboros/tools/parallel_review.py`): the configured reviewer
     slots (`OUROBOROS_REVIEW_MODELS`; duplicate model ids are valid independent
     slots) review the staged diff against `docs/CHECKLISTS.md`. Quorum is adaptive
     to the configured count via `config.adaptive_quorum` (2-of-N for N≥3, both for
     N=2; a single configured reviewer is honored as a loud
     `single_reviewer_no_diversity` degraded mode — the default config ships 3
     reviewers / 2-of-3). A configured-≥quorum-but-fewer-responded shortfall stays
     a loud infra quorum failure.
   - **Scope review** (`ouroboros/tools/scope_review.py`): one or more scope slots review
     completeness and cross-module consistency with touched context plus a
     generated repository Atlas (`review_context_atlas.compile_review_context_atlas`).

Triad and scope reviewers run concurrently via `concurrent.futures.ThreadPoolExecutor`
(orchestrated in `ouroboros/tools/parallel_review.py`). The caller receives one
combined verdict with all findings in a single round. Scope review findings block
only when `OUROBOROS_REVIEW_ENFORCEMENT=blocking`; advisory mode downgrades them
to warnings by operator policy. Scope review ALWAYS actually runs for the ≥1M
blocking reviewer (v6.30.0 guaranteed-fit): the assembler walks a deterministic
degradation ladder — full atlas → compact atlas (durable `context_manifest`
keeps full per-file coverage) → a `required` artifact the atlas cannot fit is a
FAILURE TO ASSEMBLE (typed `budget_omitted` row naming artifact and reason,
`required_artifact_omitted` pack status, no review of the remainder), which the
ladder answers by shrinking the fixed part and retrying → touched files degrade to
diff-only, freely degradable ones first and largest-first within each tier (an artifact owed in
full is reached only after the `-U0` rung), with an explicit `TOUCHED FILE BUDGET DEGRADATION NOTE` (their full
changes stay visible in the staged diff, and those paths are DECLARED to the
atlas via `diff_only_included`, so the durable coverage row records the dropped
snapshot rather than claiming the file is fully in the prompt; legal only for
merely-touched files — a `prompts/`-class or canonical artifact declared
diff-only, or any required artifact over the per-file 1MB cap, is the same
typed `budget_omitted` assembly failure). If the remaining staged diff is the
only oversized part, its unchanged hunk context may be removed with `-U0` while
every file/hunk identity and every added/deleted line remains. Triad uses the
same one-pass principle: before dispatch it may replace duplicated full touched
snapshots with a disclosed path manifest, then remove unchanged diff context.
Every step is a disclosed omission
(P1). If even the irreducible prompt (checklist + canonical docs + staged diff)
cannot fit the blocking reviewer's window — or the ladder exhausts every step
and a required artifact still cannot be assembled — the commit fails CLOSED with
`fixed_overflow` (a sub-floor reviewer records `budget_exceeded`) — split the
diff or shrink the required surface; there is no silent skip on the blocking
path, and no review of a pack a required artifact is missing from. Which of the
two it was is carried on the status and worded by ONE derivation
(`_ladder_terminal_cause`), so neither authority branch tells the owner to split
a diff that cannot shrink an unchanged required artifact — and when both causes
hold at once (the refusal that dropped the artifact was itself a hard-budget
overflow) both are reported with the mixed remedy, since either single-cause
remedy is false about the other half. The shared `REVIEW_PROMPT_TOKEN_BUDGET` / `_SCOPE_BUDGET_TOKEN_LIMIT`
(920K estimated tokens) is the INPUT-size SSOT, but scope review also reserves
`_SCOPE_MAX_TOKENS` for OUTPUT inside the reviewer's 1M context window plus
substantial tokenizer headroom (currently 155K tokens):
`_SCOPE_INPUT_TOKEN_LIMIT = min(920K, 1M − _SCOPE_MAX_TOKENS − margin)`.
The cap is model-aware on two axes. (1) Tokenizer DENSITY: Claude-family reviewers
tokenize code-heavy packs at ~2.5 chars/token (~1.58x the chars/4 estimate), and since
v6.80.0 that ratio is MEASURED per model at the physical send boundary and stored in
the `token_density` namespace of `capability_evidence.json` — the hand-set
`CLAUDE_REAL_TOKENS_PER_ESTIMATED = 1.65` and the `is_claude_family_model` substring
gate are DELETED (a multiplier table is a perpetual-staleness anti-pattern; facts are
measured or honestly unknown). `calibrated_input_token_limit` takes the STRICTEST of
the 920K budget cap, `(window − output_reserve) / density`, and the historical
`window − output_reserve − tokenizer_margin`, so it never exceeds the previous cap; a
model with no observation uses the documented conservative cold-start density and
therefore sizes DOWN. That cold-start density bounds the COLD path ONLY —
`resolve_token_density` returns `measured × safety` with NO cold floor once an
observation exists, because the constant is Claude-derived and flooring the measured
path with it would permanently charge a lighter tokenizer for Claude's density.
"Measurement can only ever TIGHTEN a cap" is enforced PER MODEL IDENTITY in the store
instead: `record_token_density` keeps the RUNNING MAXIMUM, and one normalized model
identity collects observations from every surface that uses that model, so a run of
prose-dominated doc-only packs
measuring ~1.1 cannot hand the next code-heavy scope pack a LOOSER cap than today's;
the historical absolute-margin form bounds every cap regardless. `_effective_scope_input_limit` computes it PER CALL — the former
import-time `_ANTHROPIC_SCOPE_INPUT_TOKEN_LIMIT` (≈545K) froze the pre-measurement
value for the whole process. (2) WINDOW: a known reviewer window from Capability
Evidence (`_scope_window` -> `ouroboros.capability_evidence`; no
static per-model table, v6.33.0) replaces the assumed 1M with reserves scaled to the
window (`_window_scaled_reserves`) so a small-window slot keeps a positive input
limit, and it returns PROVENANCE (`confirmed` | `asserted` | `stale_unverifiable` |
`unknown_conservative` | `designated_default_sentinel`) so diagnostics stop calling a
conservative fallback — or an expired record — "known". (v6.87.44) SIZING and
AUTHORITY are separate answers about one route and travel in one typed
`ReviewerWindow`: `sizing_window()` may be optimistic (a declined review is a certain
loss), while `blocking_authority_allowed` demands SOURCED, CURRENT, ≥1M evidence via
`capability_evidence.confirms_at_least(..., require_fresh=True)`. Freshness is an
explicit argument of that one owned predicate — `is_known(ev, require_fresh=...)` —
and (v6.87.45) NO surface restates it: the scope floor, the save-time
`review_capability_notices` twin, the Max-context gate, both context-fit route
decisions and `ContextFitPlan` (an evidence-shaped record, so the predicate applies
to it directly) all call it. Gates that AUTHORIZE pass `require_fresh=True`; the
Max-context gate, which would DOWNGRADE the owner's own horizon, deliberately does
not — a provider blip must never erase a prior confirmed record. The save-time notice
is offered on a route-affecting SETTINGS CHANGE; it is not a scheduled staleness
watch, so it precedes the commit-time twin only when the owner passes through
settings. The designated-default sentinel is a SIZING number and carries no
authority; a model acquires none from its name. How often a route may be re-probed
belongs to `probe`'s TTL alone (confirmed 24h / failed 10 min): a process-lifetime
"already probed" memo in `reviewer_window` outlived the record it produced, so an
install that stayed up past the TTL could never RE-source its reviewer and blocked
every commit for the rest of the process's life (v6.87.45).
Whether scope review applies at all is decided by the owner-only
`OUROBOROS_CONTEXT_MODE`, read as the OWNER-SELECTED value (`get_owner_context_mode`).
There is no floor CONFIG any more: `OUROBOROS_SCOPE_REVIEW_FLOOR` is deprecated and
ENFORCEMENT-INERT since v6.80.0 — its owner endpoint, contract field and self-lowering
guards are kept intact and the stored value is preserved and echoed with a deprecation
notice, but nothing consults it and there is no getter for it. In `max` a known sub-1M scope reviewer has advisory-only
verdict authority and cannot satisfy the blocking gate, even after a clean response;
if the irreducible canonical-docs prompt cannot fit, or the provider's real tokenizer
rejects it as oversized, the gate fails CLOSED without an authoritative verdict.
Non-size provider failures remain fail-closed. In `low` no reviewer is called and a
typed `skipped_low_context_mode` row is recorded instead — the owner's policy coupling,
never a coverage claim. `docs/CHECKLISTS.md` remains the single source of truth for review items;
do not duplicate or fork checklist policy here.

### External PR readiness is not commit authorization

`scripts/run_external_review.py --contributor` reuses the production triad and
scope substrate for a non-committing proposal review. It is structurally
different from `commit_reviewed`:

- the exact current target-base commit must be an ancestor of the clean,
  committed proposal head;
- reviewer slots, efforts, and scope floor come from literal shipped defaults
  in that target-base checkout, local overrides are ignored, every reviewer is
  routed through OpenRouter, and enforcement is blocking;
- Claude advisory is excluded; the hermetic deterministic test preflight still
  runs before triad + scope. Its child pytest receives only disposable
  data/settings/repo roots: live `OUROBOROS_*` behavior overrides and
  secret-class environment values are scrubbed;
- external contributors do not allocate `VERSION` or release-only carriers.
  The typed `version_bump` and `changelog_and_badge` items are recorded as final
  squash-landing obligations when they are the only critical findings; no other
  checklist item can be demoted this way;
- evidence binds target base, proposal head/tree, diff hash, target config, and
  resolved actors in a redacted shareable packet. A proposal that changes the
  review substrate cannot self-attest fast-path readiness and requires a
  trusted maintainer rerun.

The result is `READY_FOR_INTEGRATION`, not merge authority. Maintainers apply a
coherent author-attributed squash onto current `ouroboros`, choose the actual
MAJOR/MINOR/PATCH version, synchronize all P9 carriers, and run the ordinary
production review against the exact landing parent/tree/VERSION/tag binding.
If the target advances, that final binding is rebuilt and reviewed again.

The commit gate is intentionally one-pass: one substantive SESSION OR CALL per
triad slot and one per scope slot. Do not reuse task-acceptance retries, generic
capability resolvers, chunk fan-out, or degraded Low retries to multiply P3
calls. On an `api_chat` slot an exception or empty response may repeat the
identical request once on the same slot/model, and both invocations share a hard
two-physical-send rail. On an `agent_session` slot the unit is the hosted
session: its internal tool calls and model turns belong to the harness and are
never counted as review calls, at most two session LAUNCHES may be reserved for
the slot (a second only when the first produced no transcript at all), and a
resend over bad OUTPUT never restarts the session — format recovery is local
light-model extraction over the transcript already collected, which is not a
review call and consumes none of the slot's sends. A persistent
error/empty/invalid result blocks either way. The operator
wrapper must call this production path, preserve full raw actor output and refs,
report known/unknown cost neutrally, exit nonzero on incomplete review, and never
substitute the configured reviewer model.

Preferred workflow for non-trivial edits: choose the right edit tool first —
`edit_text` for one exact replacement, `edit_batch` for several exact
replacements or a counted replace-all (each edit declares how many occurrences
it expects; a mismatch aborts the whole batch before anything is written), `apply_patch` for
scattered multi-file changes (context-anchored hunks, validated across all files before the first write),
and `write_file` for new files or intentional full rewrites (overwrites return
the diff vs the previous version and block syntactically invalid .py/.json
unless forced) — then `advisory_review`, then `commit_reviewed`
immediately on the final diff. After 3 genuine review-verdict blocks of a
byte-identical staged diff, `commit_reviewed` refuses further attempts
(`attempt_cap_reached`) before spending another triad+scope run — change the
diff, provide a `review_rebuttal`, or escalate to the owner.

Review preflight tests are hermetic. Any pytest run launched by
`advisory_review` or `commit_reviewed` must execute in a disposable git worktree
with a temporary `OUROBOROS_DATA_DIR` / `OUROBOROS_SETTINGS_PATH`, a temp
`PYTHONPYCACHEPREFIX`, and no inherited `OUROBOROS_MANAGED_BY_LAUNCHER`. Tests
may read the live source checkout as the candidate snapshot, but they must not
write the live repo or live `data/`. `tests/conftest.py` must also rebind
already-imported state/queue/worker roots inside each pytest process and fail
closed if a state or evolution-campaign write resolves to the captured live data
root. Explicit subprocess environments preserve their own isolated data root or
receive the parent's disposable root and pytest markers; scrubbing unrelated
variables must not reopen the live default. Setting only `OUROBOROS_DATA_DIR` is
insufficient for process-global roots.

Self-modification durability is local-first. A successful reviewed local commit
is the persistence boundary; `origin` push and CI are optional follow-up signals.
Missing `origin` is not a failed evolution. `managed` is the official
update/provenance remote and must not become the personal self-modification push
target.

Autonomous restarts must not erase active self-evolution. If an evolution task
requests restart while work is dirty and not yet represented by a reviewed local
commit, the runtime must preserve a rescue/transaction pointer and pause or stop
the campaign rather than `rescue_and_reset` and continue as if nothing happened.
Explicit owner restart remains an operator action with broader authority.

The full pre-commit review checklists live in **`docs/CHECKLISTS.md`** —
the single source of truth (Bible P7: DRY).

This section defines what "DEVELOPMENT.md compliance" means in practice — it is the
detailed expansion of the `development_compliance` item in `docs/CHECKLISTS.md`.

### DEVELOPMENT.md Compliance Checklist

Before every commit, verify the following:

#### Naming Conventions
- [ ] Modules and variables use `snake_case`
- [ ] Classes use `PascalCase`
- [ ] Constants use `UPPER_SNAKE_CASE`
- [ ] Names are self-explanatory

#### Entity Type Rules
- [ ] **Gateway** (if present): contains ONLY transport. No business logic, no routing.
- [ ] **Tool** (`{verb}_{noun}`): thin LLM-callable wrapper. Validates input, formats output.

#### Module Size & Complexity
- [ ] Module stays near one context window (~1000 lines target; 1600 hard gate unless explicitly grandfathered debt)
- [ ] No method exceeds the practical target (150 lines) or the hard gate (300 lines)
- [ ] Total Python function count stays under the current smoke hard gate (consult `ouroboros/review.py::MAX_TOTAL_FUNCTIONS` for the active value; bump with a comment if a feature requires more headroom)
- [ ] No function has more than 8 parameters
- [ ] No gratuitous abstract layers (Bible P7)

#### Structural Rules
- [ ] New Tool? `get_tools()` exports it using the `ToolEntry` pattern from `registry.py`, an explicit entry is added to `ouroboros/safety.py::TOOL_POLICY` (`POLICY_SKIP` for trusted built-ins, `POLICY_CHECK` for opaque or outward-facing ones), AND the intended visibility is declared in `ouroboros/tool_capabilities.py` (`CORE_TOOL_NAMES`, local-readonly/acting subagent allowlists, parallel/truncation sets as appropriate). If workspace tasks should see the tool, update the workspace allowlist in `tools/registry.py` too. Without the policy entry the tool falls through to `DEFAULT_POLICY = POLICY_CHECK` and pays a light-model LLM call per invocation, and without the capability/allowlist wiring a packaged/visible tool can still be unreachable to subagents or workspace tasks. **A tool that WRITES the repo working tree needs the GUARD surfaces too, not only the visibility ones:** add it to `_ROOT_ARG_REPO_WRITE_TOOLS` (the single set behind the acting-no-workspace fence, the protected-write gate and the acting root-enum narrowing) and make sure its target paths are canonicalized — via `_PATH_NORMALIZED_TOOLS` if it takes a top-level `path`, or via `canonical_repo_relative_path` + `_payload_write_paths` if its paths ride inside the payload. Visibility lists are all green while these are missing, so the gap does not surface as a failing test: `apply_patch`/`edit_batch` shipped a protected-path bypass that way (a guard reading `repo/BIBLE.md` while the write landed on `BIBLE.md`). Tests must exercise the REAL guard chain — a test that monkeypatches the resolver proves the mechanics, not the fence.
- [ ] New Gateway (if extracted)? Contains no business logic, only transport.
- [ ] New memory/data files? Should they appear in LLM context (`context.py`)?

#### Skill Repair Task Constraints
- Skill repair tasks use structured `task_constraint.mode="skill_repair"`, not prompt markers.
- In repair mode, edit paths are payload-relative: `plugin.py` means the selected `data/skills/{external,clawhub,ouroboroshub}/<skill>/plugin.py`.
- Use `edit_text` for one exact replacement and `write_file` only for new files or intentional full rewrites with `root=skill_payload`. (`edit_batch`/`apply_patch` are repo-lane tools and do not accept `root=skill_payload`.)
- Finish repair with `skill_preflight` and `skill_review`; grants and enablement stay owner-controlled.
- Repair mode is a stricter UI lane, not the only path for skill authoring. In `runtime_mode=light`, ordinary chat tasks may edit explicit `data/skills/{external,clawhub,ouroboroshub}/<skill>/...` payloads via `write_file`/`edit_text` with `root=skill_payload`, `bucket`, and `skill_name`. Explicit repo/data paths keep their own address space and ignore stale short-form args. Core/repo paths, `data/skills/native/*`, `data/state/skills/*`, marketplace/provenance sidecars, and direct `run_command` writes to repo targets remain blocked.
- New path checks for skill edits must use `ouroboros.contracts.skill_payload_policy` rather than reimplementing bucket/path traversal logic in each tool.

#### Native-Risk Extension Dispatch
- `type: extension` skills with reviewed isolated dependency envs must not import `plugin.py` or execute handlers inside `server.py`, even when the dependency tree looks pure-Python. Payload-native marker files (`.so`, `.dylib`, `.dll`, `.pyd`) also force child dispatch as defense in depth, but opaque native payloads remain subject to the skill-review checklist and are not newly allowed by this runtime fallback.
- Keep the split explicit: no-dependency pure-Python extensions may use `extension_loader`'s in-process PluginAPI path; isolated-dep/native-marker extensions are cataloged and dispatched by `extension_process_runner` short-lived child processes.
- Tool, HTTP route, and WebSocket handler proxies must return normal tool errors / HTTP 502 / WS log messages on child crash, invalid JSON, timeout, or abort. A child `SIGABRT` is a handled extension failure, not a server crash.
- Child processes must use scrubbed env, per-skill grants, per-skill isolated deps, process-group tracking, output caps, and timeout cleanup. Do not add fallback code that imports native-risk plugin modules in the host process.

#### Task Contract Resource Policy
- When a task contract declares `resource_policy.protected_artifacts`, enforce it as a typed affordance policy in every runtime mode: execute-only black-box references may be run, but byte reads, copy/hash/static introspection, tracing, and debugging against declared paths are blocked. Do not add benchmark-specific command gates.
- Observable Acceptance Claims (`task_contract.acceptance_claims`) are advisory, task-general criteria (`claim` / `surface` / `support` / `priority`). The `support` text names expected evidence only; reviewers may credit actual support only from host-built `support_refs`; v1 links these refs through verification receipts by `criterion_id` and carries receipt-attested details such as `matched`, `artifact_lifecycle`, and missing-after facts. Standalone artifact/source refs without a claim-linked receipt are a deferred v2, not evidence by themselves. Do not turn these claims into a hard task-acceptance gate or a benchmark-specific enum taxonomy.

#### Devtools And Benchmark Tooling
- `devtools/` is tracked operator code, not runtime core. It may contain benchmark harness adapters, smoke runners, and reproducibility helpers that should be versioned with Ouroboros, but runtime modules under `ouroboros/`, `server.py`, web modules, and build scripts must not import it.
- `devtools/` is not included in the Python runtime package discovery; it is repository-side operator tooling, not an installed dependency of the Ouroboros app.
- `devtools/` is not an immune-system bypass. If a commit touches `devtools/`, triad/scope reviewers inspect those touched files fully. Unrelated `devtools/` files use the Atlas `excluded_dir` disposition and stay coverage-manifest-only in broad packs so benchmark harness code does not drown normal core reviews.
- Benchmark adapters must preserve official task instructions, official scoring/evaluation commands, and official artifact formats. They may build predictions, launch official runners, normalize logs, or aggregate official outputs, but must not implement benchmark-specific prompt hacks, routing hacks, or replacement scoring. CARVE-OUT (explicit, owner Q20/Q22, v6.79.0): a NEGATIVE or DISCLOSURE-shaped instruction of the `GAIA_ANTI_LEAK_INSTRUCTION` / `GAIA_EPISTEMIC_INSTRUCTION` class is not a prompt hack and belongs in the adapter. Such text may forbid answer-key lookup and require the agent to say when a claim is unverified; it must NOT tell the agent how to solve the task, name the benchmark, or reveal answer-shaping hints, it must be a single SSOT constant appended identically by every solver of that harness (so cross-harness comparisons stay fair), it must be disclosed in that benchmark's METHODOLOGY.md, and it must stay OUT of `prompts/SYSTEM.md` and out of the typed task contract — a runtime-wide grounding duty would make ordinary tasks search the web for facts the model already knows.
- Generated benchmark runs, datasets, container outputs, logs, predictions, and submissions belong under `/Users/anton/Ouroboros/bench_runs/` or another explicit output root outside `repo/`, never under `devtools/`.
- SWE-bench Pro patch capture must be provenance-based, not filename-pattern-based: pre-existing base-untracked files may be excluded from `model_patch` by a base snapshot, while genuinely new agent-created files must remain included. Keep diagnostic status artifacts honest about whether they are pre-filter or post-filter.
- SWE-bench Pro install transports must fail fast with typed infra reasons for permanent environment failures (for example musl pyexpat/pip/server-import failures) instead of retrying them as provider/network transients.
- Run provenance is a GATE, not a report. Build the run manifest through `benchmark_run_manifest()` ONCE, immediately after argument parsing or readiness — that call is where the clean-seed gate runs (`require_clean=True` by default; the launcher flag `--allow-dirty-seed` records the exception) — then write it to disk at once (a later refusal must still leave a durable record of what was refused and why), keep that dict, augment it, and rewrite it at the end with the final outcome. A manifest written after the work cannot refuse anything, and one that never records how the run ended cannot be audited. Nothing that costs money or mutates shared state (image pulls, volume writes, container starts) may precede that gate. `benchmark_run_manifest` sits exactly on the 8-parameter limit: further knobs ride through `metadata`/`**overrides`, never as new parameters. Since v6.75.0 that lifecycle is TWO SHARED SEAMS, not a per-launcher convention: `admit_benchmark_run(manifest_path, **manifest_kwargs)` builds, persists and only then enforces (a refusal raises `BenchmarkAdmissionRefused`, a `RuntimeError` carrying the manifest, so the record of what was refused is durable), and `finalize_run_manifest(manifest_path, manifest)` is the single finalization seam — a context manager whose yielded mapping is merged into `manifest["extra"]` and written on every exit path, including an escaping exception (`outcome: crashed` plus a typed `error`). Never pair `benchmark_run_manifest()` with your own `write_json()` in a launcher again: a meta-test in `tests/test_devtools_benchmarks.py` names every migrated launcher and fails if one does. ADMISSION IS THE OUTER BOUNDARY: everything in a launcher before `admit_benchmark_run()` must be argument parsing and pure local derivation — no filesystem assertion, no docker, no subprocess, no network, no state mutation, and nothing that can itself refuse evaluated inside the admission call's argument list (Python evaluates arguments first, so such a refusal writes no manifest at all). The same meta-test walks each launcher with `ast` and fails on a denylisted pre-admission call; put the check inside the finalization block and record a typed `refusal` instead. THE RECORDED `exit_code` MUST BE THE PROCESS'S EXIT STATUS: after writing one, RETURN it — do not re-raise, because an escaping non-`SystemExit` exception exits the process with 1 no matter what the manifest says. Recording 1 and letting the exception escape is equally valid; a parametrised behavioural test in `tests/test_devtools_benchmarks.py` drives every migrated launcher into a refusal path and compares the two. AND THE RECORDED OUTCOME MUST COME FROM THE HARNESS'S OWN RESULT, NEVER FROM ITS EXIT CODE: `inspect eval` returns 0 for an eval that raised and `harbor run` returns 0 for a job whose trials all errored, so a launcher that maps `returncode == 0` to `outcome: completed` records an infrastructure failure as a successful run (v6.81.0 GAIA smoke: every sample killed by a sandbox setup timeout, nothing scored, manifest `completed`/`exit_code: 0`). Read the harness's own artefact — inspect's log `status` plus per-sample `scores`, harbor's trial rewards — and keep three facts APART: a harness that raised, a harness that finished and scored nothing (both infrastructure zeros, both non-`completed` and non-zero exit), and a harness that scored genuine zeros (a real result, `completed`). An unreadable artefact is a fourth, fail-closed case, not a pass: unknown success is not success.
- Provenance compares FOUR DIFFERENT identities and never conflates them: the immutable seed stamp (equality), the live HEAD of an evolving volume (a LINE OF DESCENT via `merge-base --is-ancestor` — an evolution run legitimately moves HEAD forward), the version a running server reports over HTTP, and THE GRADED-SPEC PIN — the third-party dataset checkout that supplies both the instruction handed to the agent and the evaluator that scores it. The graded-spec pin obeys the same "gate, not report" rule as the manifest itself: a recorded `matches_aligned_commit: false` that no code refuses on is a footnote, and a footnote cannot protect a number (v6.81.2 OSWorld probe: recorded in all 1706 task manifests, read by nobody, 21 of 75 tasks graded against a three-week-older task JSON, making the old-vs-new delta cross-scale). Two consequences for adapters and launchers: the variable naming that checkout must be REQUIRED rather than defaulted, because a multi-lane launcher forwards a fixed whitelist of environment variables and a pin variable missing from it is silently replaced by each lane's own default; and the per-task manifest must record the checkout root plus its commit so verification reads the artifact instead of the intent. A config-drift check that has no pin key proves nothing about the pin. A refusal must be a ONE-SHOT step, never part of a polled readiness probe whose loop reads any non-zero rc as "not ready yet". Volume seeding stays conditional: an existing `.git` is an evolved volume and is never re-seeded.
- New helpers for provenance/preflight belong in `devtools/benchmarks/common/manifests.py`, which stays stdlib-only at module import (launchers include a container-side agent that must not gain a runtime-package dependency) — import from `ouroboros` lazily inside the function that needs it.

#### Light Mode External Deliverables
- `runtime_mode=light` is a self-modification boundary, not an OS sandbox. User-visible deliverables are allowed when they are outside the Ouroboros repo/control-plane.
- Preferred flow: `task_drive` for scratch, `artifact_store` for canonical deliverables, and `user_files` for the owner's visible copy (for example `Desktop/report.html`). `write_file(root=user_files)` and declared process `outputs` must register/copy canonical task artifacts. Rewrites of the same user-visible source keep the previous canonical artifact in non-manifest history with last-5 retention; history is for recovery, not a second deliverable list.
- `run_command`/`run_script` `scratch=[...]` (v6.52.2) is a DISTINCT channel from `outputs=[...]`: it declares EPHEMERAL in-workspace verification files (a throwaway test the agent writes, runs, and deletes — e.g. an in-package test that must live in the repo to compile). Scratch is exempt from the undeclared-output guard, never registered as an artifact, confined to the cwd, honored for NEW files and (v6.56.0) for ADOPTED existing untracked in-cwd files — adoption records the file's sha at declaration time through the SSOT `artifacts.record_task_scratch`, so the patch exclusion applies only while the content still matches (tracked files, paths outside the cwd, and paths outside a git worktree stay blocked; a real edit can never hide behind a scratch declaration) — and excluded from the workspace patch via `.scratch_manifest.json` (`headless.write_workspace_patch_artifacts`). Re-declaring a manifest path is idempotent. The undeclared-output guard verifies candidates POST-exec by stat (exists + mtime ≥ start−slack), so a mere path MENTION (import strings, CLI flags, heredoc bodies) is not a write. Use `outputs` for deliverables, `scratch` for throwaway verification — never overload one for the other.
- `run_command`/`run_script`/`start_service` may use cwd under `active_workspace`, task-scoped `task_drive`, task-scoped `artifact_store`, and external `user_files` where the active profile permits it. In light direct tasks, omitted `run_script.cwd` defaults to task scratch instead of the Ouroboros repo; long-running services in light must use an explicit external/task/artifact cwd. Declared service `outputs` are copied into the task artifact store when the service stops.
- `run_script` temporary files are created under the active workspace when the task is workspace/executor-backed, then removed after execution. Do not run workspace scripts from the system repo temp path; relative imports, generated files, and toolchain discovery must observe the same cwd the user requested.
- Declared process outputs may be files or directories. Directory outputs are copied to the canonical artifact store as a bounded manifest plus zip archive; hidden/control/credential-shaped files, excessive file counts, and excessive byte sizes fail closed instead of leaking through artifact registration.
- In external workspace mode, light-mode self-repo dirty checks snapshot the system repo, not the active workspace. Task-local git operations inside the external workspace are allowed when the task requires them; Ouroboros repo/data paths remain structurally protected, and workspace patch artifacts are captured against the preflight git base.
- `claude_code_edit` is RETIRED (D10, owner-approved migration, phase 6.4): the SDK edit gateway's job moved to the delegated coding path — a mutating subagent (`schedule_subagent`) whose nanny drives the session with `delegate_start`/`delegate_wait`/`delegate_cancel`, on the owner's subscription when a harness route is configured. Compatibility is one-way and permanent: a saved task contract carrying `disabled_tools=["claude_code_edit"]` also withholds the successor `delegate_start` (registry `_disabled_tools`), and the frozen `GET /api/claude-code/status` + `POST /api/claude-code/install` endpoints stay — the Claude runtime still powers the api-route advisory review. Do not resurrect the tool name.
- Do not recommend `runtime_data/uploads`, skill payloads, or owner state directories as generic artifact transport.

#### Runtime Cleanup / Retention
- All age-based garbage collection of disposable runtime artifacts shares ONE
  owner knob, `OUROBOROS_GC_RETENTION_DAYS` (default 7, hard max 365), and the
  cutoff/clamp math in `ouroboros/retention.py` (`age_cutoff`,
  `clamp_retention_days`, `get_gc_retention_days`). Do not hand-roll
  `now - days * 86400` or `max(1, min(days, 365))` in new prune code; reuse the
  helpers.
- The three former per-subsystem keys
  (`OUROBOROS_SUBAGENT_WORKTREE_RETENTION_DAYS`,
  `OUROBOROS_SERVICE_LOG_RETENTION_DAYS`,
  `OUROBOROS_HEADLESS_TASK_RETENTION_DAYS`) are deprecated and migrated into the
  unified key on settings load (`config.load_settings`). Do not reintroduce them.
  If a subsystem ever genuinely needs its own lifetime, name it
  `OUROBOROS_<SUBSYSTEM>_RETENTION_DAYS` and add it as a fallback in
  `retention.LEGACY_RETENTION_KEYS`, but prefer the unified knob.
- Prune functions keep an explicit `retention_days=` parameter for tests/special
  cases; only the default (None) resolution reads the owner knob. Startup prunes
  are wired from one place (`server.py`).
- Durable artifacts are NOT age-pruned and must stay out of the GC sweep: genesis
  projects (`OUROBOROS_SUBAGENT_PROJECTS_ROOT`) and forensic observability blobs
  (kept compressed indefinitely).

#### Live Subagent Task Constraints
- Live subagents are scheduled only through the existing `schedule_subagent` tool.
  Its public schema is strict: `objective` and `expected_output` are required;
  `role`, `context`, `constraints`, `memory_mode`, `model_lane`, and the typed
  delegation-budget grants `delegation_intent`, `may_mutate`, `may_fan_out`, and
  `max_children` (v6.37.0 C3.1) are optional; v6.50.0 adds a closed-enum
  `required_capabilities` list as schedule-time admission data (not a frozen
  task-contract field). The booleans `may_mutate`/`may_fan_out`
  are parsed with the strict `normalize_bool` (the string `"false"` is NOT truthy),
  and the child's budget only ever NARROWS within the parent's
  (`_narrow_child_delegation_budget`): recursion authority (delegate/fan-out/
  max-children) is AND-ed with / capped to the parent's, and `may_mutate` is gated
  by the parent ONLY when the parent is itself a subagent (so a root honors its
  explicit opt-in while a read-only subagent cannot escalate). Do not reintroduce
  public `parent_task_id` or `description` arguments; lineage comes from `ToolContext`.
- Live `memory_mode=shared` is disabled. Keep `forked` and `empty` as the only
  live subagent modes unless a later design adds sanitized shared-context v2.
- External `/api/tasks` and CLI requests must reject forged
  `delegation_role=subagent`; only `schedule_subagent` may create subagents.
- `task_constraint.mode="local_readonly_subagent"` must be enforced twice:
  schema discovery exposes only the local-readonly allowlist, and registry
  execution rejects forbidden calls even when invoked manually.
- Mutative ("acting") subagents (`task_constraint.mode="acting_subagent"`) are
  opt-in via `schedule_subagent(write_surface=...)` plus the master toggle
  `OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS` (default ON in advanced/pro, OFF in light).
  `active_tool_profile` must fail closed: an invalid/missing surface, or a
  delegated subagent with a broken constraint, resolves to read-only — never to
  `self_modification`/`operator_control`. Acting children write only inside their
  surface (`self_worktree`/`external_workspace`/`genesis`) and keep commit,
  review, runtime control, tool-enable, skills lifecycle, and cognitive-memory
  writes blocked; `external_tool_grants` is deny-by-default for extension/MCP.
- `genesis` is a from-scratch deliverable surface: the supervisor provisions a
  fresh EMPTY git repo under the durable projects root
  (`OUROBOROS_SUBAGENT_PROJECTS_ROOT`, outside `repo/`/`data/`) via
  `subagent_worktrees.provision_genesis_project`. It is NOT the system repo, so
  protected-path discipline does not apply; it is durable (never GC-pruned and not
  in the worktree registry) because the project directory IS the deliverable. The
  parent does NOT `integrate_subagent_patch` a genesis project into the live body;
  the returned `workspace.patch` (diff from the empty seed commit) is only a record.
- `self_worktree` is a checkout of the system repo: keep protected-path write
  discipline AND protected shell-write guards active for it (no workspace bypass),
  permitting protected edits only in pro AND with `protected_paths_grant`. The
  worktree root must stay outside `repo/` and `data/` (guarded in
  `subagent_worktrees.provision_worktree`).
- `external_workspace` acting children write in the SAME active external workspace
  as the parent. `integrate_subagent_patch` verifies the child's declared
  write/root lineage and that reported files are present, then records a verdict;
  it must not re-apply the patch into the shared workspace because the edits are
  already there.
- The parent is the SOLE committer of the live body. Acting children return a
  `workspace.patch`; the parent applies a chosen patch with
  `integrate_subagent_patch` (manifest-first, sha256-verified, 3-way apply, writes
  a `subagent_patch_verdict` artifact, invalidates advisory) and then runs its own
  `commit_reviewed`. Routing is top-only — never integrate a descendant directly
  into the live repo; bubble patches up one parent at a time
  (`ctx.active_repo_dir()`).
- The supervisor (`_resolve_subagent_constraint`) is the authoritative gate that
  validates the toggle/surface and provisions/validates `self_worktree`;
  `server.py` startup calls `subagent_worktrees.prune_orphans()` (git has no
  worktree GC). Worktree mutations use a dedicated cross-process ops lock, not the
  drive-scoped repo git lock.
- `task_constraint` boolean parsing must be strict; strings such as `"false"`
  are false, never truthy through Python's `bool("false")`.
- The effective delegation budget is a pure admission reducer: declared
  `delegation_budget`, explicit `required_capabilities`, and unresolved
  structured non-advisory `delegation_constraint` rows are reconciled before a
  child runs. Scheduler back-pressure rows may be advisory telemetry (for
  example `queued_behind_active_cap`) and must not block later queued children
  below the hard ceiling.
  Do not infer child needs from objective prose; the LLM declares them via the
  closed enum. Do not add fields to `contracts/task_contract.py` for this.
- `delegation_constraint` is a typed task-tree beacon with a structured payload
  (`constraint_id`, directive, scope, rationale). Consumers must read the payload,
  never parse the text. Overrides require an explicit reason and are recorded as
  decision rows.
- Subagent changes must keep writes, commits, review mutation, runtime control,
  tool expansion, skills lifecycle, and shell blocked — except bounded task-tree
  coordination via `tree_note`/`tree_read`, parent-only
  `override_delegation_constraint`, and bounded media projection such as
  `extract_video_frames` writing derived frames only under the task artifact store
  (`artifact_store/video_frames`) through a host-owned command shape (the permitted
  local coordination/projection paths; not arbitrary workspace or repo mutation).
  Nested readonly
  `schedule_subagent` recursion is allowed only within configured depth/cap
  limits; depth bounds nesting only and never rewrites a
  descendant's lane. Enabled/reviewed extension tools and enabled MCP tools may remain
  callable by owner policy, subject to inherited `task_contract.allowed_resources`
  such as no-network/no-web.
- A NEW `plan_task` scout wave is admitted before launch, and only a NEW one: worker capacity,
  the shared review-wave budget gate (`review_helpers.review_wave_budget_gate` — no second budget
  authority), and a consumable window. Each scout's deadline is bound to that window (the wave's
  shared cutoff minus the finalization grace and a margin, the reserve capped at a fraction of a
  short window) instead of inheriting the parent deadline verbatim, and a wave whose window has
  already closed is refused with a typed reason rather than started and then omitted. The
  recovery/collection path is NEVER gated: those handoffs are already paid for, so declining them
  would abandon spend. With `OUROBOROS_MAX_SUBAGENT_DEPTH=0` scouts are refused by the same
  delegation gate as any other child, and `plan_task` then completes on its existing
  `degraded_evidence` path — no wedge, no second wave.
- Runtime-internal scheduling knobs do NOT become `schedule_subagent` parameters.
  `control._schedule_task` is `(ctx, internal, /, **params)`: `params` is validated against
  `control.schedule_subagent_param_names()`, which is DERIVED from
  `control.schedule_subagent_properties()` — the single source the published JSON schema in
  `get_tools` is also built from (unknown keys get the strict v6 refusal). There is no separate
  mirror of the schema to keep in sync, and none may be reintroduced: a hand-maintained copy is
  correct only until one side gains a parameter, at which point the handler refuses something the
  model can see or accepts something it cannot (BIBLE P7). Internal-only options instead travel in
  the POSITIONAL-ONLY `internal` mapping keyed by `_INTERNAL_SCHEDULE_OPTIONS` — structurally
  unreachable from tool-call JSON, which is keyword-only. That is what keeps the handler inside the
  <8-parameter contract. Add an internal knob to that closed set, never to the signature and never
  to the public schema. The test of membership is WHO DECIDES, not who currently calls: an option
  the runtime decides belongs in the closed set, and an option the parent LLM is the right judge of
  belongs in the public schema. `deadline_at` was the set's only member until v6.87.7, when it moved
  to the schema on exactly that test — the parent is what knows when a child's handoff stops being
  useful, and a scout deadline was only ever runtime-internal because `plan_task` happened to be its
  first caller. The set is empty today; it stays because it is closed and an unknown key in it still
  fails loudly.
- `plan_task` planning scouts use the same live-subagent worker pool and one
  shared terminal-or-cutoff wait boundary. Poll in
  `OUROBOROS_PLAN_TASK_SWARM_TIMEOUT_SEC` slices, but wait for every started scout
  until it becomes terminal or the existing
  `OUROBOROS_PLAN_TASK_SWARM_MAX_WAIT_SEC` ceiling. At that
  boundary, send every ready non-empty handoff to the reviewer and include every
  omission with its precise terminal/wait reason; missing evidence must never be
  silently presented as complete. Capacity, scheduling failure, or a normal
  cutoff does not trigger an extra inline model call: the omissions manifest goes
  directly to the configured reviewer panel. Repeated calls with the same plan fingerprint
  reuse the existing durable `plan_review_state` wave and never schedule a second wave, including
  when the first wave ended without a usable handoff. Only reviewer-included
  handoffs become consumed. Late terminal results are retained as audit evidence
  with `affects_review=false` and do not reopen the plan. If an included child
  changes after its exact snapshot enters the reviewer prompt, keep the old hash
  non-authoritative, persist the review once with a bounded stale-binding warning,
  and treat the newer child result as audit-only rather than paying for replay.
- `read_file(root=runtime_data)` and `list_files(root=runtime_data)` secret/control-file denials are subagent-scoped.
- Browser isolation for local-readonly/acting subagents (DNS fail-closed): block
  non-HTTP(S) schemes, private/link-local/reserved/unspecified and numeric-obfuscated
  literal IPs, unresolved hostnames, and hostnames resolving to any blocked IP — before
  goto, after redirects, and in route handlers. Loopback HTTP(S) is ALLOWED EXCEPT the
  Ouroboros control-plane ports (agent API / local-model / host-service, the configured
  `LOCAL_MODEL_PORT`, and the actual bound `state/server_port`); `file://` is ALLOWED
  only under the task's explicit `workspace_root` (symlink/traversal-safe), denied
  otherwise. `evaluate` JS stays unavailable to subagents; `vlm_query` /
  `analyze_screenshot` are available. (Relaxed in v6.24.0 for local UI/build inspection;
  control-plane, private-range, and DNS-rebind denial preserved. See ARCHITECTURE.md.)
- Effective task status belongs in `ouroboros/task_status.py`. Do not duplicate
  child-drive result merge or terminal-status logic in gateways/tools; use
  `load_effective_task_result`, `effective_task_result`, and bounded wait
  helpers. `wait_task` and `get_task_result` results must remain untruncated
  (full child handoff); `wait_tasks` returns a compact structural projection
  per child (task_id, status, cost_usd, child_result_sha256, outcome_axes,
  result, trace_summary, capability_delta when disclosable, duplicate_of) with
  a disclosed `tasks_note` pointer —
  the full envelope stays in `task_results/<id>.json` (addressable by
  `child_result_sha256`; `get_task_result` returns the full result text plus
  trace/outcome summaries). Do not re-inline forensics (trace_refs, loop_outcome,
  verification_ledger) into the batch projection.
- `forward_to_worker` may write only to validated running tasks whose lineage
  belongs to the current task/root, and must route forked/empty child subagents
  to the child-drive mailbox.
  Do not broaden generic data-tool behavior for normal tasks while fixing
  subagent isolation.
- The pre-final handoff reminder is a compact effective-status snapshot. Full
  untruncated child handoff belongs to `get_task_result` and `wait_task`
  (`wait_tasks` is a compact batch projection — see above). Do not add shared
  ledgers, automatic memory merges, or new settings/endpoints unless the
  accepted plan explicitly calls for them.
- A delegating parent must not produce a clean no-tool final answer while direct
  children are still running and undecided. One bounded absorption reminder is
  allowed; after that, finalization is best-effort (`children_unabsorbed`) rather
  than clean. This is an outcome-honesty rule, not a new wait loop.

#### Page Header Layout
- Top-level page chrome (`renderPageHeader`, tab strips, primary actions) must sit outside the scrolling content region.
- Pages use an outer flex column plus an inner `<page>-scroll` body with `overflow-y:auto`. Skills, Widgets, Settings, and Chat follow this pattern.
- Page icons come from `web/modules/page_icons.js`; do not paste divergent SVGs into individual page modules or the navigation rail.
- Primary page actions, including Refresh, live in the `renderPageHeader({ actionsHtml })` slot on the right. Do not add ad-hoc refresh rows inside scroll bodies.
- Non-chat top-level pages use `.app-page-glass` for the shared dim/brand backdrop. Header padding should stay compact; if a page needs more space, simplify its copy rather than growing the chrome.
- A new top-level page that scrolls its header together with content violates the architecture mirror: fix the layout, not the symptom.
- Top-level tab/pill buttons are a single design-system control: `renderTabStrip` + `.app-tab-strip` + `.app-tab` + the `--pill-*` CSS variables in `web/style.css`. Do not redeclare per-page tab padding, font size, border radius, or active styling in page CSS files.
- Scrollable page bodies use the shared `.scroll-fade-y` mask when content can pass under fixed page chrome. Do not copy/paste custom gradient masks into page modules; extend the shared class if the fade rhythm changes.
- Masonry-style widget packing uses `web/modules/masonry.js::applyMasonry`. Do not reintroduce CSS Grid row packing (`align-items: start`) for unequal-height widget cards; it leaves row gaps under shorter cards.
- Widget card ordering is a host UI preference. Persist it through `/api/ui/preferences` and `data/state/ui_preferences.json`; never rewrite extension manifests or widget declarations to store owner layout.
- New visual dimensions should become CSS variables first (`--pill-*`, `--button-*`, `--page-header-*`, etc.) and then be consumed by shared classes. Hardcoded page-local dimensions are review debt unless the component is genuinely unique.

#### Setup / Onboarding Layout
- The first-run wizard is a compact multi-step flow. At the default desktop
  window size it should not force scrolling merely because the access step has
  several provider fields; use responsive two-column field grids where width
  allows and keep step copy short.
- Onboarding and Settings share the setup contract. If a key is typed in the
  current unsaved wizard payload, UI diagnostics must account for that in-memory
  value instead of warning from stale saved settings alone.
- Owner switches should expose the semantic choices the owner can actually make.
  For `OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS`, Settings presents explicit On/Off;
  the empty runtime-default state remains a backend/default behavior, not a third
  owner-facing button.
- One capability, one section. Delegation (`OUROBOROS_SUBAGENT_HARNESS`) and the
  write permission (`OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS`) share Models → Subagents
  (`web/modules/subagents_settings.js`), beside Reviewer Slots: both answer "where
  and how far do subagents run". Never render a second control over the same
  settings key — two controls carry two drafts, and the last one collected wins.
  The delegated-run MODEL is the owner's default, authored here as the `=model`
  tail of the same key from engine discovery ("Engine default model" = empty
  tail); reasoning effort stays derived per call, and a hand-written `:effort`
  remainder rides through verbatim with no control over it.
- A control the owner cannot use is worse than none. With no coding-agent
  subscription connected the Subagents section says so and points at Providers →
  Harness Accounts instead of rendering a delegation toggle whose every dispatch
  would silently fall back to an API child. Harness lists come from the accounts
  panel's own source (`accountRows` over `/api/claudexor/status`) — one catalog
  path, one login-capable discriminator.

#### LLM Call Rules
- [ ] New LLM calls go through the shared `LLMClient` / `llm.py` layer — no ad-hoc HTTP clients or direct provider SDKs outside that layer. **Exception (v5.7.0+):** skill / extension `plugin.py` modules may call providers directly because they have not yet been migrated to a host-mediated `api.invoke_llm(...)` bridge. When that bridge lands, the exception goes away. Runtime callers (anything inside `ouroboros/`) must still use `LLMClient`.
- [ ] Every core-mediated physical provider send goes through `usage_accounting.execute_physical_attempt[_async]`: reserve, mark dispatched, then settle/unresolve. A transport retry is a new attempt. `llm_usage`, state, and UI counters are projections carrying attempt ids, never a second monetary authority. Provider tier pricing and any empirical tokenizer margin affect only a known reservation; settlement prefers actual provider usage/cost. Unknown price reserves `None`, remains nullable in usage events, and never blocks a model merely because its tariff is unavailable. An external skill with granted model-provider credentials is explicitly unknown/unmetered when it bypasses core transport—not `$0`; an ordinary spawned process must not be mislabeled as monetary work.
- [ ] Hold the usage-ledger cross-process lock only for budget check, validated append, and fsync. Never hold it over network I/O. Preserve a paid response if settlement persistence fails and leave an honest dispatched/unresolved bound.
- [ ] Before dispatching any post-task consolidation or synthesis worker, read `usage_breakdown` once for the whole root subtree and pass the same loop-local snapshot to summary and reflection. It is explicitly non-final (`cost_final=false`, `cost_with_children_partial=true`) and carries child-inclusive accounted cost, reservations, unresolved upper bound, unknown/unmetered count, ledger integrity, and capture time. A read failure is unavailable/null, never `$0`. Consolidation, summary, and reflection model spend belongs only to the existing terminal checkpoint; do not add another ledger or reconciliation LLM call.
- [ ] Runtime notices after the first user/assistant/tool turn are user notices, not new `role=system` messages. `LLMClient` defensively demotes non-leading system messages at the provider boundary; source call-sites should still append `[SYSTEM NOTICE]` user turns so provider payloads, local templates, and prompt authority stay consistent.
- [ ] Keep stable policy/governance first and dynamic evidence last. Prompt-cache support is deliberately narrow: direct OpenAI `prompt_cache_key`, OpenRouter `session_id` (or a caller-declared `cache_affinity` for surfaces whose rounds repeat with changing evidence, e.g. review), and one exact retry without the named parameter only when the provider explicitly rejects that parameter. Do not add provider hops, body rerouting, or a generic cache/retry framework.
- [ ] **Cache-friendliness invariant.** Any change that builds or reorders prompt/context content must not degrade prompt caching: never insert dynamic values (timestamps, hashes, round counters, per-task identity) into a stable cached prefix, never move stable governance content behind dynamic evidence, and never strip or bypass existing `cache_control` markers / cache-affinity keys. Review surfaces mark their stable prefix via `review_helpers.cached_prompt_blocks` (block-level `cache_control` with the review TTL); the boundary each builder reports must contain only byte-stable content. The acceptance substrate (v6.74.0) marks TWO segments — byte-stable governance, then the task-stable contract (goal/scope/checklist/policy) — leaves the per-pass evidence tail unmarked (it changes every pass by design; the exact review binding is useless as a breakpoint because an unchanged binding never makes a second call), keeps slot identity at the TAIL of the mutable part, and asserts `review_substrate.assert_cache_breakpoint_cap` (≤4 declared breakpoints) on the final payload — reuse that assertion in new multi-segment builders. (v6.77.0) A builder no longer places tool markers or orders TTLs itself: `llm.LLMClient._normalize_payload_cache_ttl` finalizes the ASSEMBLED provider payload at every physical-send boundary — it promotes earlier existing breakpoints to `1h` when any later one asks for `1h` (Anthropic requires longer TTLs first; a 5m tools marker before a 1h system marker is a hard 400), marks the last tool schema when the tools segment carries none, and on a >4 payload keeps the four earliest anchors while dropping tail markers with a disclosed `prompt_cache_breakpoints_reduced` usage field. Declare your intended TTL on the blocks you own and let the finalizer order them; the assertion above remains the LOUD builder-side layer (the finalizer is the fail-soft transport guard), and routes that cannot carry markers are left byte-identical, so never re-add a per-builder marking site. This is checklist item `cache_friendliness` in `docs/CHECKLISTS.md`.
- [ ] OpenRouter reasoning continuity belongs to OpenRouter conversations only. Direct/local payloads strip OpenRouter round-trip metadata; OpenRouter payloads with `reasoning_details` disable provider fallback to avoid endpoint-bound thought-signature corruption.
- [ ] Claude Agent SDK sessions (the api-route advisory since D10 retired the edit gateway — the edit path's system-prompt file handoff died with it) must preserve the full governance prompt; do not truncate BIBLE/ARCHITECTURE/DEVELOPMENT/CHECKLISTS to avoid argv or transport limits.
- [ ] Delegated (subscription-harness) work is accounted on its OWN ledger row:
  `usage_accounting.record_subscription_session`, which feeds the separate sessions/quota
  axis (`subscription_sessions` / `subscription_windows`). Its cash has THREE states and
  only the first is final: a DISCLOSED ZERO (`spend_usd=0.0`) settles at `cost_usd=0.0,
  cost_final=True` and leaves the projection final — the case this row kind exists for;
  an ESTIMATED amount (`spend_estimated=True`, the engine's `spendEstimated`) rides as
  money but never as finality; an UNDISCLOSED spend (`spend_usd=None`) is `cost_usd: None`
  and counted unknown/unmetered, never a confident `0.0`. Token counts are the same rule
  on the usage axis: `None` means no harness reported it, which is not a run that used
  zero. Do NOT reuse `record_unmetered_external_dispatch` for any of them — it also drops
  the sessions/quota axis. The nanny's own model calls remain ordinary metered attempts
  and keep rolling into the task projection; a subscription session is not counted as a
  physical provider call. D29: the APPLIED `credential_profile_id` and `access_profile`
  the engine's `authRoute` receipt / `effectiveAccess` disclosed ride the durable row (and
  the settled event) BY DEFAULT — empty when telemetry predates the receipt, never
  invented — so "which account paid, under which access" is answerable from the ledger
  row, the settled event, and (for reviewer slots) the last-execution file. Those are
  three separate stores, deliberately not joined into one applied receipt.
- [ ] `cost_final` on a projection is a COUNT of open rows (`non_final_rows`), never a
  truthiness test on a dollar sum: a reserved/dispatched/unresolved row, a settled row
  with an unknown price, and a settled row its writer marked non-final are each open
  however little they cost — `$0.00` is a real reservation for `provider="local"` and a
  real estimate for a delegated run. `non_final_rows` is returned beside the flag because
  a projection can be non-final with every dollar bucket at zero, and a flag without its
  cause is not reconstructible.
- [ ] A spent SUBSCRIPTION WINDOW is `subscription_window_exhausted`, a TRANSIENT class
  carrying `reset_at`, scheduled against that instant rather than through the
  60-second-capped exponential backoff. Do not fold it into `quota_exhausted`, which is
  classified permanent — correctly so for a billing refusal (402, no credits) and wrong
  for a window whose only cure is waiting.
- [ ] Provider failures must be classified before retrying the same request.
  Quota/auth/billing, hard bad-request, and request-too-large/context failures
  are non-retryable as-is: record the exact category and surface a recovery hint
  instead of burning rounds on identical calls. Transient rate limits/timeouts may
  still use the normal retry path.

#### Timeout & Wait Control
- [ ] For cognitive/long-horizon work (subagent waits and review),
  prefer **progress-aware / re-decidable waits** over a single fixed cutoff that
  discards in-flight work. A passive wait that does not kill should stay in its
  window while the observed task is non-terminal **and** progressing, up to a
  generous ceiling, then fail closed with a precise structured reason. Progress
  ADMITS the wait to keep waiting; it does not hand control back per event —
  returning on each advance woke a full-context nanny round every poll interval
  (measured: 18 rounds, 861k prompt tokens, for a run that was doing fine), so
  the observations are carried back once, at the window's expiry.
- [ ] Planning-scout collection is deliberately different: every started scout
  shares one terminal-or-cutoff boundary, and the reviewer receives explicit
  omissions at that boundary without a heartbeat-based early stop or inline
  fallback model.
- [ ] The wait/continue/stop decision must be a **structured fact** — terminal
  status plus heartbeat freshness from `queue_snapshot.json` — not a keyword or
  regex over content (Bible P5). Use `task_status.py` terminal-status helpers and
  the supervisor heartbeat, not string matching.
- [ ] Fixed **kill**-timeouts (hard task/tool ceilings, watchdog) still exist as
  the outer safety bound and get sensible ceilings under high-reasoning models;
  progress-aware waiting tunes the *passive* wait, it does not remove the watchdog.
- [ ] New numeric timeout constants are an SSOT in `config.py` `SETTINGS_DEFAULTS`
  with a getter and env registration; do not scatter magic wait numbers across
  call sites.

#### Loop / State-Machine Changes
- [ ] Changes to `loop.py` or other task state-machine logic include adversarial tests for malformed output, false-completion prevention, replay/log durability, and failure modes — not just the happy path.
- [ ] Audit/checkpoint rounds must not silently reuse the normal final-answer path unless that invariant is explicitly tested and documented.
- [ ] Keep a complete loop-local `DeliveryCandidate` once a substantive answer exists. A service round may return `keep`, or `replace` plus the complete replacement answer; allow one repair for malformed control, then preserve the prior complete answer and mark finalization degraded. A service notice alone does not change evidence. Owner messages, tool effects, child results, and verification receipts advance the evidence revision and require fresh delivery/acceptance binding. Finalize task-scoped service outputs/errors before host acceptance and require a complete replacement when that evidence changes; keep the `finally` path as idempotent cleanup only. This control must not bypass verification, acceptance, safety, skill-finalization, deadline, child-handoff, the unconditional `FINAL ANSWER:` latch, or the task-level answer protocol.
- [ ] Every direct child result needs an exact-hash disposition through the existing `tree_note(kind="decision")` tagged payload (`type=child_result_disposition`, child id, `integrated | irrelevant | deferred`, complete-result SHA-256; note text is rationale). The typed task-tree row is the sole authority; task-result disposition fields are derived reads, never a mirrored write. The join-ledger helper alone validates lineage and current content. Stale or malformed payloads change nothing. `deferred` suppresses only the unchanged reminder and forces an honest degraded/best-effort terminal answer until the item is resolved. Explicit cancellation wins a late-completion race and bounded child scratch is removed without preserving another copy.
- [ ] Host task acceptance is root-only. Queued/headless/scheduled roots are reviewed in `auto` and `required`; direct eligibility is the union of `outcomes.turn_has_reviewable_effects` and a typed deliverable/criterion. Ordinary read-only tool activity, pure conversation, and meta/routing controls are not reviewed, and child reviews remain advisory. Eligibility must use structured facts, never keywords (Bible P3/P5). For an eligible root under `auto|required`, agent-callable `task_acceptance_review` validates/stores evidence and optional agent disposition but makes zero reviewer calls; it returns `deferred_to_host_acceptance`, `authoritative=false`, and the evidence revision. The call itself never widens eligibility; child and `off` behavior remain unchanged.
- [ ] Before root acceptance, atomically fence new descendants under the queue lock and prove recursive subtree quiescence from the existing task-status SSOT. Split-drive ACK, subtree, and acceptance-timing reads/writes use canonical `budget_drive_root`. Preserve the prior verdict until the replacement is recorded. A revision must explicitly reopen the fence; terminal/degraded outcomes seal it.
- [ ] The host runs the authoritative acceptance panel once per unchanged candidate-hash/evidence-revision/fence binding. Task-acceptance actors receive one substantive call and at most two physical attempts total. Record transport status, parse status, and valid-response semantic verdict separately, with actor model/provider, role, coverage, panel id, quorum contribution, reason, enforcement impact, and binding hashes. Public task/event/UI records receive only the compact projection; full model payloads remain in private audit storage. `adaptive_quorum` applies; any contributing FAIL fails, DEGRADED abstains (the reviewer verdict vocabulary `PASS|FAIL|DEGRADED` is NOT narrowable — `_contract_valid_actors`, the deliberate-DEGRADED capsule rail and the host's core-overflow DEGRADED all depend on it), and no quorum is a terminal HOST decision. The host acceptance decision itself is written ONLY by `loop._set_acceptance_decision` and has exactly three owner-facing states — `accepted | revision_requested | finalized_unaccepted` — each with a typed `reason` from an existing structured fact; an unknown status fails closed to `finalized_unaccepted` keeping its raw token as the reason. When you add a writer, add its reason to the closed set AND check every value-keyed reader: `outcomes.derive_loop_outcome` keys the deadline-reserve degradation on the status+reason PAIR, and breaking that pairing is a silent false green. The agent may write only `agent_disposition`/`agent_rationale`, merged into the host decision, never replacing it. Clean requires PASS + solved + supported criterion evidence. Chat and Logs must use the same severity reducer, and degraded review or best-effort/degraded objective must never render as green solved. Do not add task scope review or reuse the commit gate.
- [ ] The acceptance improvement loop is a reviewer-authored DIALOGUE (v6.74.0): obligation identity comes from the reviewer's typed `disposition_kind`/`obligation_id` (an unknown re-raise id fails closed to `new`, disclosed — never a silent fresh hash id); a re-raise reopens the row WITHOUT wiping the agent's argument (`previous_disposition`/`previous_reason`/`reopened_count` survive into the evidence catalog and the obligations clause); termination beyond a clean PASS/accepted rebuttal happens ONLY via the reviewers' quorum `dialogue_status` judgement reduced over ALL contract-valid actors (`aggregate_dialogue_status` — never `_contributing_actors`, which drops a DEGRADED slot's vote) or a real rail — no host counters, no answer/verdict hashes, no keyword gates (P5). Changes here must cover: malformed reviewer output, unknown/stale `obligation_id` on a re_raise, partial panel failure, multi-slot dialogue-status disagreement (the reducer's precedence), replay/restart durability of obligation rows, false completion, and the backward-compatible default when the new fields are absent.
- [ ] An explicit `max_improvement_passes` binds under every legacy policy. Required+Blocking without one has no local count cap, but real deadline/budget/lifecycle rails remain. The first acceptance review reserves at least 200s; later passes use the canonical event-derived `max(floor, 1.5×EWMA)` (`alpha=0.5`). Only the root runs global post-task synthesis once and persists one phase checkpoint in the canonical `budget_drive_root`. Recovery is startup-only: replay `pending_once`, degrade indeterminate `running` without another paid call, and let the normal supervisor copy-back/artifact path materialize child results without overwriting a terminal canonical phase.

#### Cognitive Artifact Integrity
- [ ] Cognitive artifacts (identity.md, scratchpad, task reflections, review outputs, pattern register) must NOT use hardcoded `[:N]` truncation. If content must be shortened, include an explicit omission note (e.g. `⚠️ OMISSION NOTE: truncated at N chars`).
- [ ] `BIBLE.md`, `docs/ARCHITECTURE.md`, and `docs/DEVELOPMENT.md` are **core governance artifacts**. All primary reasoning flows (triad review, consciousness, advisory pre-review, deep review) include them as first-class sections — see the "Core Governance Artifacts" table. If you add a new reasoning flow, it MUST follow this contract, not rely on touched-file inclusions.

---

*This section is the authoritative definition of "DEVELOPMENT.md compliance" referenced in the `development_compliance` item in `docs/CHECKLISTS.md`.*

---

## Managed Update Rule

- Keep the local work branch and the official update feed separate. The local
  branch is `ouroboros`; `OUROBOROS_UPDATE_CHANNEL` maps Stable to `main`, QA to
  `ouroboros-stable`, and Development to `ouroboros`. QA and Development follow
  their branch tips. Stable resolves the newest plain `vX.Y.Z` tag whose commit
  is present in both `main` and `ouroboros-stable`.
- A preflight chooses one exact official target SHA. Apply must bind to the
  disclosed base/target, close new writers, drain existing direct/ephemeral
  turns, stop workers and tracked services, then re-plan before mutation.
- Clean fast-forwards land the official SHA directly. Git also builds clean
  merges for divergent local history, with parents = reviewed HEAD + official
  target. Dirty local work never enters that history: the apply stashes it and
  restores it as uncommitted content (boot finalize on success, the pre-update
  tree on rollback; a conflicting restore keeps the stash and discloses the
  recovery command). The reviewed assisted resolver runs only when Git reports
  a real conflict; filenames do not create a second update policy. Hard reset
  is an explicitly confirmed recovery only.
- The authorized resolver stages the complete merge, including tracked binary
  files. Review receives their exact staged mode/blob/size plus the HEAD and
  official MERGE_HEAD object ids; deletions carry an explicit absent stage and
  exact parent identities. Missing exact metadata still blocks. This exception
  does not weaken the ordinary commit pipeline's binary policy.
- Write the update transaction before mutation. Reopen writers only after a
  verified abort/rollback or a healthy restart. An unverified rollback keeps
  its retryable phase plus the full failure evidence; a legacy `gate_blocked`
  marker retries rollback on boot. Delayed evolution cleanup also acquires the
  same update lock and honors this admission owner; it must not stash/reset
  behind the fence. Managed merge tests pass before restart; the ordinary
  self-modification commit/tag/test/push ordering remains unchanged.
- Manual Restore reuses the same writer fence and pins the previous HEAD on a
  local recovery branch before reset. Promotion resolves the development SHA
  once and uses that exact SHA for both the local QA ref and any remote push.

---

## Mutation Attribution Rule

- Attribution is evidence, not exclusion. The host captures a `system_repo`
  baseline in the existing task result when a queued root task starts and a
  terminal candidate snapshot at outcome derivation; blockers (pre-existing
  dirty changed, stale/missing baseline, failed scan) ride into review and
  acceptance evidence for the LLM panels to weigh. Do not turn them into
  structural outcome vetoes, and do not add a lease/holder service, a second
  ledger, or runtime writer keyword scanners.
- Git staging is attribution-based. `paths=None` means the clean-at-baseline
  candidate set, an explicit list must be its subset, and empty never means
  `git add -A`. Preserve pre-existing user dirt as excluded evidence. Whole-tree
  staging belongs only to already-typed managed update/release transactions and
  the official SWE-Pro capture helper. Contexts without a captured baseline
  (manual ToolContext, external dry-run review) keep the legacy staging
  contract.
- Resolve unversioned Python only for `run_command`, `run_script`,
  `start_service`, and run-kind `verify_and_record`, once before the shell guard.
  Guard and handler must receive identical argv. Do not rewrite explicit paths,
  versioned interpreters, shell bodies, or remote execution, and never install a
  dependency in response to `ModuleNotFoundError`.
- Skill Review ordinals and provenance stay in `review_job.json` and the
  append-only `review_history.jsonl`: allocate under the lifecycle lock, consume
  a round only after actual start, write one terminal row per `job_id`, and
  compute legacy ordinals at read time without rewriting history.

## Process Custody Rule

Long-lived OS processes (anything `subprocess.Popen`-ed or `mp.Process`-ed
without a bounded wait in the same call) **MUST** be spawned through
`ouroboros.process_custody.spawn_supervised(cmd, drive_root=..., purpose=...,
scope=...)` — or, when an existing manager owns the Popen call, registered via
`record_process(...)` write-through immediately after spawn. The custody
ledger (`data/state/process_ledger.jsonl`) is what lets the orphan reaper find
children after an abrupt worker/server death; an unledgered process orphans
invisibly and forever. Scopes: `task` (dies with its task), `session` (dies
with the server generation), `daemon` (genuine launcher-managed lifecycles,
e.g. `server_restart_fallback` — reaper keeps them, only pruning dead entries).
Skill **companions** also record `daemon` scope but are the documented
exception: `reap_orphaned_processes` reaps a companion (`purpose
companion:<skill>:<name>`) when its owning skill is **uninstalled** OR the entry
is from a **foreign (dead) server generation** (`CompanionSupervisor.start()`
always re-spawns a fresh pid, so a generation-crossing match is a stale
duplicate). This is **log-only by default** (`enforce_companion_reap=False`
emits a `process_would_reap` event instead of killing) and **fail-safe**:
`live_owner_skills=None` (unknown install set — incl. a momentarily empty skills
dir, coalesced to `None`) means keep-all, never a mass-kill, and same-session
companions of installed skills are always kept so the live `CompanionSupervisor`
stays their sole owner. The reaper kills strictly by (pid, start_time,
cmd_sha256) fingerprint — never add command-line-class matching, which would let
a dev instance reap a packaged instance's processes.
`tests/test_process_custody.py` enforces the chokepoint with an explicit
allowlist for bounded synchronous helpers.

## Platform Abstraction Rule

All platform-specific code **MUST** go through `ouroboros/platform_layer.py`.

### Shared State-File Helpers

Durable JSON state files should use the SSOT helpers in `ouroboros/utils.py`:
`atomic_write_json(path, payload, trailing_newline=False, fsync=False)` for
write-then-rename persistence and `read_json_dict(path)` for dict-shaped JSON
reads. `write_text_atomic(path, content, fsync=False)` is the underlying shared
atomic FULL-OVERWRITE primitive (temp-sibling + `os.replace`, existing permission
bits preserved, crash leaves the old file intact); `atomic_write_json` layers JSON
serialization on it, and `write_text` (the plain text overwrite helper) routes
through it, so every overwrite routed through these helpers is crash-safe — prefer
them over a bare `Path.write_text` for any full-file overwrite. Appends are
intentionally NOT atomic (they extend in place). Lockfile acquisition should go through
`platform_layer.acquire_exclusive_file_lock` /
`release_exclusive_file_lock` rather than reimplementing `O_CREAT|O_EXCL`
loops in feature modules.

Narrow exceptions are allowed only when the file's contract is not JSON-object
state or intentionally has extra durability semantics: `supervisor/state.py`
keeps `atomic_write_text` for mirrored `state.json` / `state.last_good.json`
text writes, and `ouroboros/config.py` keeps its settings-file lock because the
settings path is bootstrapped before broader runtime helpers should depend on
settings state.

### What counts as platform-specific

- Direct use of: `os.kill`, `os.setsid`, `os.killpg`, `os.getpgid`, `signal.SIGKILL`, `signal.SIGTERM`
- Unix-only modules: `fcntl`, `resource`, `grp`, `pwd`
- Windows-only modules: `msvcrt`, `winreg`, `ctypes.windll`
- `subprocess` with platform-conditional flags: `start_new_session`, `creationflags`
- Hardcoded path separators (`/` or `\\`) in filesystem logic (use `pathlib` instead)

### Rules

1. **All platform-specific calls live in `platform_layer.py`** — the rest of the codebase imports cross-platform wrappers from there.
2. **Platform-specific modules are imported inside `platform_layer.py` only**, guarded by `IS_WINDOWS` / `IS_MACOS` / `IS_LINUX` checks.
3. **No top-level imports of Unix-only or Windows-only modules** outside `platform_layer.py`. If you need `fcntl` — you're in the wrong file.
4. **Use `pathlib.Path`** for filesystem paths. Never construct paths with string concatenation using `/` or `\\`.

### Enforcement

- **AST-based test** (`tests/test_platform_guard.py`): scans `.py` files under `ouroboros/`, `supervisor/`, and `server.py` for:
  - Top-level imports of platform-specific modules (`fcntl`, `msvcrt`, `winreg`, `resource`)
  - Direct `os.kill`, `os.killpg`, `os.setsid`, `os.getpgid` attribute access
  - Direct `signal.SIGKILL`, `signal.SIGTERM` attribute access
  
  Not scanned by the AST guard: `launcher.py` (immutable outer shell, intentionally excluded) and subprocess flag patterns (`creationflags`, `start_new_session`). For subprocess isolation, use `subprocess_new_group_kwargs()` and `subprocess_hidden_kwargs()` from `platform_layer.py` — enforced by code review and the `cross_platform` checklist item.
- **Pre-commit review**: checklist item `cross_platform` (#15) catches violations during code review.
- **CI matrix**: tests run on Ubuntu, Windows, and macOS to catch runtime failures.

### Adding new platform-specific code

1. Add the cross-platform wrapper to `platform_layer.py`.
2. Import and use the wrapper in callers.
3. Add platform-conditional tests if behavior differs across OSes.

---

## Design System

Ouroboros uses **glassmorphism** as its visual language. All interactive surfaces follow this pattern:

```css
background: rgba(26, 21, 32, 0.62–0.88);
backdrop-filter: blur(8–16px);
border: 1px solid rgba(255, 255, 255, 0.06–0.12);
```

### Floating overlay transparency (v5.7.0+)

Floating chrome that overlays scrolling content (chat header, sticky tab
strips inside Settings/Dashboard/Skills, files preview gradient) follows ONE
shared formula and never relies on a separate fade-overlay element:

1. The chrome element is `position: absolute` with the appropriate edge
   (`top: 0` for headers, `bottom: 0` for bottom overlays, etc.) and
   covers the whole horizontal axis.
2. Its background is a **single 4-stop linear gradient** that fades from
   the dense brand background at the chrome's anchor edge to fully
   transparent at the opposite edge.
3. `backdrop-filter: blur(10–14px)` is applied on the same element
   (the host always supplies `-webkit-` prefix in lockstep).
4. **A CSS `mask-image` matching the gradient direction fades the blur
   in lockstep**: `mask-image: linear-gradient(0deg, black 0%, black 70%, transparent 100%)`.
   This is the rule that prevents the visible "glass edge" the v5.6.x
   chat dock had — without the mask the blur creates its own hard
   horizontal line at the gradient's transparent stop.
5. The scrollable surface reserves enough top/bottom padding so content is
   reachable outside the overlay's dense zone.

**Chat input dock exception:** the bottom composer intentionally splits the
formula. `#chat-input-area` is a compact absolute bottom overlay with a
darkening gradient only (no wrapper `backdrop-filter`), so message text fades
under the dock without a tall smeared blur band. The active textarea itself
is the frosted surface (`background: rgba(26,21,32,0.55);
backdrop-filter: blur(20px)`). `#chat-messages` reserves bottom padding
through `--chat-input-reserve`, which JS sets from the actual dock height
plus a small buffer; mobile adds safe-area on top of that. Top padding uses
the same mechanism: `--chat-header-reserve` is measured from the real
`pageHeader.offsetHeight` (wrapping two-row headers on narrow viewports) by
the same `updateMessagesPadding()`/ResizeObserver pair.
`updateMessagesPadding()`
preserves scroll stickiness only; it must not mutate DOM padding.

### Glass control rules

- Composer, toolbar, segmented, and widget-reorder controls use the same glass
  grammar: translucent dark background, subtle border, blur, and bounded radius.
  Do not add transparent text-only pills for primary actions.
- Desktop chat composer controls stay inside the single frosted text-entry
  surface. On mobile, Swarm and Low/Max move above the textarea so text
  width remains usable, while Send stays inside the field.
- Button and segmented-control labels use `letter-spacing: 0` and stable
  dimensions. If a label does not fit on mobile, shrink the control group or
  move it to another row; do not reserve a large textarea padding gutter.
- Drag/drop affordances are stateful CSS classes (`drag-active`, `drag-over`,
  etc.) on the host control/card. Do not use inline styles for visual feedback.

### Browser/mobile verification

- For every visible UI change, open at least one relevant real consumer flow in
  an available browser and actually inspect the rendered evidence with vision.
  Saving a screenshot without viewing it is not visual verification. Select
  states, viewports, and additional engines according to the change's concrete
  risk; there is no universal browser or device matrix.
- `browse_page` defaults to Chromium. When actual iOS/Safari behavior is a
  material risk, use `engine="webkit"` plus a Playwright iPhone device
  descriptor; a 390px Chromium viewport is a responsive-layout check, not an
  iOS Safari check. Mobile and WebKit are not global requirements and are not
  installed automatically for acceptance.
- An unavailable optional engine is not degradation by itself. If the visual
  evidence the implementer judged necessary cannot be obtained, report the
  result as degraded/best-effort and name the missing evidence.
- Browser packaging keeps engine availability explicit per platform. macOS
  packages bundle Chromium headless shell only; Playwright WebKit uses the
  managed runtime cache on first `engine="webkit"` use because the WebKit
  payload contains nested `.framework`/`.xpc` bundles and `.tbd` stubs that do
  not survive the signed PyInstaller app layout as a simple embedded payload.
  Linux, Docker, and Windows builds still bundle Chromium and WebKit. Do not
  re-add bundled macOS WebKit unless codesign/notarization is proven end to end.

Do NOT introduce a separate `.chat-bottom-fade` (or analogous overlay)
layer. A second fade layer compounds the gradient and can produce a visible
"double dim" especially over short messages.

### Navigation sidebar (v6.32.0 redesign)

The desktop navigation is a left `#primary-sidebar` of ROWS (not an icon
rail): each destination is a `.nav-row` (icon + label) and the Projects group
is a `.nav-section-toggle` that expands a data-driven list of project rows
(`renderProjectsNav` in `web/app.js`, fed by `/api/state`). `syncNavigationState`
keeps the active row, the Projects expand/collapse, and the open project panel
in sync. A project opens as a right split panel on desktop and a full-width
overlay with backdrop on mobile, hosting a full chat instance over the ONE
shared WebSocket (client-side fan-out by `chat_id`). On mobile the sidebar
collapses behind an "Open navigation" toggle (drawer), NOT a horizontal bottom
bar. Spacing/typography come from the shared design tokens in `web/style.css`
(no per-screen hardcoding); global controls (restart/panic + the "More" menu for
consciousness/evolve/review) live in the chat header, not the sidebar.

The compact Projects header keeps the shared layers icon, label, unread pill,
chevron, and an always-visible `+`. Project rows expose one sibling Rename/Delete
menu, reachable by pointer and keyboard; Enter/Space open, Escape closes, focus
order stays logical, click-outside closes, and placement is viewport-safe. Name
validation uses the backend `PROJECT_NAME_MAX` SSOT (80), never a divergent UI
constant. Unread is `visible_revision > project_seen_revision`; acknowledge only
after the room has painted, and make cursor writes monotonic/server-clamped.
Routine task heartbeat telemetry must never create a bubble or unread revision.
Only typed real incidents may enter the live card/Activity plus one deduplicated
toast.

Project history is a projection of canonical chat rows, not a mirror log. A
presentation annotation sidecar may store the latest routing action/target/status
for a `client_message_id`, but it must never become routing or Project-state
authority. Deletion is fenced `active → deleting → tombstoned`; preserve id,
bindings, chat/history, folder, and memory, and never permit resurrection.

<!-- Historical (pre-v6.32.0 icon rail; superseded by the sidebar above):
The desktop `#nav-rail` used Material 3 / Apple HIG navigation-rail
spacing norms: `padding: 28px 0 16px; gap: 10px;`. The previous
`12px / 4px` was visibly cramped (the first button hugged the top edge
of the viewport). Bump these values together when adding new nav
buttons; resist tightening them.

On mobile (`@media (max-width: 640px)`) the rail flips to a horizontal
bottom bar with `justify-content: safe center`. The `safe` keyword
keeps the row centered when content fits and gracefully degrades to
flex-start when content overflows on very narrow phones. `min-width:
60px` per `.nav-btn` keeps labels like "Dashboard" from truncating in
space-evenly mode.
-->

The mobile `.scroll-tabs` pattern (settings/dashboard/skills) uses
horizontal-scroll pills with `scrollIntoView({ inline: 'center' })`
on activation so the active pill is always visible. Do not reintroduce
the v5.6.0 drill-down accordion (`settings-subtab-open` /
`settings-mobile-back`) — it traded one tap for two.

### Notifications

Transient status must use `web/modules/toast.js::showToast()`, which renders
fixed-position notifications in `#toast-stack`, top-right but below page chrome.
The offset is intentional: toasts must never cover the Chat composer or primary
page actions. Toasts must not be inserted into page content or headers, because
that shifts the interface while the person is reading or clicking. Use reserved
inline status rows only when the status belongs to a specific control group and
that row is always present (for example marketplace search status). Do not
create page-prepended banners or local wrapper aliases such as `showBanner` for
short-lived events such as review started, install queued, or grant saved.

### Accent colors

| Role | Value | Usage |
|------|-------|-------|
| Primary | `rgba(201, 53, 69, ...)` = `#c93545` | Nav buttons, chat cards, borders |
| Hover/focus | `rgba(232, 93, 111, ...)` = `#e85d6f` | Focus glow, settings hover |

Use the primary accent for new features. Avoid introducing additional red/crimson shades.

### Border radius scale

| Token | Value | Usage |
|-------|-------|-------|
| `--radius-xs` | `3px` | Micro accents (progress bars) |
| `--radius-sm` | `8px` | Small controls, filter chips |
| `--radius-md` | `10px` | Chips, log-counter pills, page-fade rules |
| `--radius` | `12px` | Inputs, inner cards |
| `--radius-lg` | `16px` | Nav buttons, chat/live cards |
| `--radius-xl` | `20px` | Logo images, large media |
| *(no token)* | `18px` | Section cards (settings, form panels) |
| *(no token)* | `24px` | Modal/wizard shells, chat input |

Use CSS variables where possible. Do not introduce new hardcoded radius values.
When a new radius value is needed, add it to `:root` in `web/style.css` first.

### Interactive states

```css
hover:  transform: scale(1.02–1.04) + border-color +1 step brightness
active: background rgba(201,53,69, 0.12) + crimson glow
focus:  border-color rgba(232,93,111,0.4) + box-shadow 0 0 0 3px rgba(201,53,69,0.10)
```

### Button conventions

All normal application buttons use the shared `.btn` base class plus exactly
one semantic variant:

| Variant | Purpose |
|---------|---------|
| `.btn-primary` | Primary action in the current surface: enable, install, update, start |
| `.btn-secondary` | Neutral secondary action next to a primary action: reload, cancel, install runtime |
| `.btn-default` | Low-emphasis utility action: refresh, details, open related view |
| `.btn-ghost` | Very quiet action on an already-strong surface |
| `.btn-save` | Persist settings or budget changes |
| `.btn-danger` | Destructive or emergency action |

Size modifiers are `.btn-xs` and `.btn-sm`; omit a size modifier for the
default medium size. Do not combine semantic variants (for
example, `.btn-default.btn-primary` is invalid), and do not invent one-off
button schemes in feature modules. Onboarding and modal buttons use the same
`.btn` variants as the main SPA.

Buttons are horizontally centered by default. If a control intentionally uses a
menu-row layout, use a named menu-item class (for example `.skills-menu-item`)
rather than overloading `.btn`.

### "Working" phase color

Use **crimson** (`rgba(248, 130, 140, ...)`) for active/working states everywhere — not blue.
The Logs page phase badges now match Chat live card colors.

### No inline styles in JS

JS modules that generate HTML must use CSS class names, not `style=""` attributes.
This is enforced by reviewer policy — `.style.*` assignments on DOM elements (e.g.
`element.style.display`, `element.style.color`) will produce a REVIEW_BLOCKED finding.
**Accepted exception — dynamic CSS custom properties.** Setting a CSS variable for a
genuinely DYNAMIC value (`root.style.setProperty('--sidebar-width', w + 'px')` for a
live drag) is the idiomatic CSS-variable theming API, not a static inline style — it
feeds a stylesheet rule rather than hard-coding a visual property on the element, and
routing it through a managed `<style>` rule re-parsed each frame would be strictly
worse. CSS-variable mutation via `setProperty('--x', …)` is therefore allowed; static
visual properties (`display`/`color`/`width`/…) remain blocked. (v6.34.0, CW10)
Existing classes (`.stat-card`, `.page-header`, `.app-page-*`, `.app-tab-*`, `.about-*`, `.costs-*`) cover common layouts.
For new top-level pages, prefer `web/modules/page_header.js` over bespoke header/tab markup.
Add new classes to `web/style.css` when needed.
Before staging any `web/modules/*.js` file: `grep -n "\.style\." web/modules/*.js`
and fix any hits.
Legacy inline assignments that already existed before a scoped change are tracked
debt, not an automatic release blocker, when the diff does not add or worsen that
style usage. Prefer paying them down opportunistically instead of expanding the
scope of unrelated UI work.

### Declarative widget UI

Extension widgets should prefer host-owned declarative render schemas.
`web/modules/widgets.js` is the single host for `register_ui_tab`
declarations: `iframe` remains sandboxed with no relaxed tokens, and
`kind: "declarative"` / `schema_version: 1` covers forms, actions, markdown,
JSON, key/value summaries, tables, progress, files, galleries,
image/audio/video media, map/calendar/kanban, and the additive `group`,
`metric`, and `callout` composition components. New common widget capabilities
should extend that declarative schema and its tests, not introduce arbitrary
skill HTML, CSS, JavaScript, chart options, or cross-widget bindings.

v5.7.0 adds one deliberate exception for rare custom UI: `kind: "module"`
loads reviewed skill-provided `widget.js` into a sandboxed `srcdoc` iframe
(`sandbox="allow-scripts"`, **no** `allow-same-origin`). The parent host
fetches the reviewed JS from `/api/extensions/<skill>/module/<entry>` and
injects a constrained `fetch` bridge that only proxies
`/api/extensions/<skill>/...` routes. This is not same-origin SPA execution;
the module cannot access app cookies or `localStorage`.

Rules for widget changes:

- `group.components` and `tabs[].components` may contain interactive
  components. Give every mounted component an explicit `id` or let the host use
  its stable tree path; never key lifecycle state by a top-level array index.
  `subscription.render` is transitively passive: forms, actions, pollers,
  streams, subscriptions, and mutating kanban remain forbidden anywhere below
  it. One widget-level disposer owns timers, streams, abort controllers, charts,
  and snapshots, and inactive tabs do not restart lifecycle work.
- Escape by HTML context: use `escapeHtmlText()` for text-node content and
  markdown fallbacks, `escapeHtmlAttr()` for interpolated attribute values
  (`data-*`, `src`, `alt`, `title`, `href`, `value`) and mixed template
  snippets, and DOMPurify only for markdown blocks.
- Media sources must be extension routes under `/api/extensions/<skill>/...`
  or explicitly safe `data:` URLs for image/audio/video MIME types.
- Long-running user actions (image/music/research generation) must use the
  declarative async job contract: start route returns `job_id`, status route
  returns `queued|running|done|error`, and the widget host resumes polling by
  `job_id` after tab switches. Do not implement long generation as a single
  foreground HTTP request that can be lost when the widget remounts.
- Download controls must use the host download helper (`data-widget-download-url`
  / desktop bridge / fetch-blob fallback). Raw in-app navigation links are not
  acceptable for downloads because desktop WebView may replace the Ouroboros UI
  with the media file.
- Forms and Settings reuse the safe field renderer/value collector in
  `web/modules/ui_helpers.js`; Settings keeps its narrow route/component
  contract. Password values never persist across renders, duplicate submit is
  blocked, and busy/error cleanup must restore the control state.
- Charts preserve unknown/non-finite values as `null`, keep `spanGaps=false`,
  expose an ARIA label and an expandable semantic table built from the same
  data, and fall back to that table when Chart.js is unavailable. Kanban drag
  and native `Move to` use the same route and `{card_id, column_id}` payload.
- Do not load arbitrary JS modules from skill directories into the SPA origin.
  `kind: "module"` is allowed only through the sandboxed iframe + parent fetch
  bridge above, and must be covered by the `widget_module_safety` review item.
- Add/update `tests/test_widgets_ui_static.py` for every new component kind or
  media policy.

---

## MCP Client Integration

The base-runtime MCP surface is a **client only** for trusted HTTP/SSE MCP
servers. It borrows external tools and exposes them through `ToolRegistry`;
it does not expose Ouroboros as an MCP server.

Rules for MCP changes:

- Keep MCP disabled by default. `MCP_ENABLED`, `MCP_TOOL_TIMEOUT_SEC`, and
  `MCP_SERVERS` are the only base settings. `MCP_SERVERS` stays in
  `settings.json` as a list of dicts; do not serialize it into env vars.
- Support only `streamable_http` and `sse` in the base runtime. Stdio MCP,
  resources, and prompts are separate architectural changes.
- All MCP tool names must be produced by
  `ouroboros.mcp_client.make_tool_name()` and must remain provider-safe
  (`mcp_<server>__<tool>`, max 64 chars).
- All URL and header validation lives in `ouroboros/mcp_client.py`.
  Do not duplicate scheme, metadata-host, link-local, auth-header, or
  control-character checks in UI/API modules.
- `auth_token` values flow only through `settings.json` and in-process
  manager state. `/api/settings` masks them, `/api/settings` POST rehydrates
  masked values from old settings, and `/api/mcp/status` exposes only
  `auth_configured`.
- MCP descriptions and tool results are server-supplied untrusted data.
  Descriptions must be wrapped before reaching the LLM, UI strings must be
  escaped, and MCP text must never be treated as policy.
- MCP tools are part of the selected initial capability envelope when MCP is
  enabled. Discovery failures must surface through an explicit capability
  omission manifest, not a silent skip. MCP tools remain blocked in
  skill-repair/heal contexts and run through `safety.check_safety` before
  dispatch.
- When changing MCP behavior, update the focused MCP tests:
  `tests/test_mcp_client.py`, `tests/test_mcp_api.py`,
  `tests/test_mcp_registry_integration.py`,
  `tests/test_mcp_settings_roundtrip.py`, and
  `tests/test_mcp_ui_static.py`.

---

## Gateway Boundary Pattern

Browser-facing backend work goes through `ouroboros/gateway/`.

- `gateway/router.py` is the single place that mounts Starlette routes for
  `/api/*` and `/ws`. Do not add new browser routes directly in `server.py`.
- `gateway/contracts.py` is the frozen frontend/backend contract. It contains
  endpoint tokens, WebSocket discriminators, and TypedDict envelope shapes.
  This file is protected by `runtime_mode_policy.py` and may be edited only in
  `runtime_mode='pro'`.
- Domain handlers live in sibling modules: `settings.py`, `control.py`,
  `files.py`, `models.py`, `extensions.py`, `marketplace.py`, `mcp.py`,
  `host_service.py`, `history.py`, `tasks.py`, `schedules.py`, `logs.py`,
  and `state.py`.
- Frontend code calls backend APIs through `web/modules/api_client.js`.
  `web/modules/api_types.js` mirrors core contracts via JSDoc so frontend
  contributors have a visible surface without TypeScript, codegen, or a build
  step.
- Any new browser endpoint must update `gateway/contracts.py`,
  `gateway/router.py`, `web/modules/api_client.js` when the UI consumes it, and
  the parity/smoke tests in `tests/test_gateway_parity.py` /
  `tests/test_gateway_smoke.py`.

---

## Build & CI

### Pytest marker lanes

Default local pytest excludes costly or environment-dependent lanes:
`integration`, `browser`, `ui_browser`, `ui_browser_docker`,
`portable_detail`, and `skill_smoke`. CI opts into them explicitly:

- `integration` runs real provider checks, including Cloud.ru when
  `CLOUDRU_FOUNDATION_MODELS_API_KEY` is configured and GigaChat when
  `GIGACHAT_CREDENTIALS` is configured.
- `browser` launches real Playwright Chromium/WebKit for agent browser tools.
- `ui_browser` launches the host-side web UI under Playwright.
- `ui_browser_docker` talks to an `ouroboros-web:test` container and must
  skip cleanly when Docker is unavailable locally.
- `portable_detail` covers build/portable artifact invariants and also runs
  inside Docker in the manual/tag CI tier.
- `skill_smoke` installs the nine pinned official OuroborosHub skills
  (list in `tests/test_skill_smoke_official.py`) from the LIVE catalog and
  validates payload/sha/provenance, manifest contract, offline
  `skill_preflight`, real pip isolated deps, and keyless command probes. It
  runs as the dedicated 3-OS `skill-smoke` CI job (stable promote / manual /
  `v*` tags) in serial pytest invocations with real network + real pip:
  red means investigate (our runtime or the published catalog broke) — there
  is deliberately no fallback-skip. Its tests must NOT carry the `serial`
  marker or join `_SERIAL_TEST_FILES`: the `and not skill_smoke` markexprs
  in quick/full-test are the barrier that keeps the lane out of those
  passes, and the no-serial rule keeps each test's lane assignment single
  and unambiguous (defense-in-depth on top of that barrier).
  The lane's Tier 6 (`test_review_grants_and_enable`) additionally exercises
  the production install→review→auto-grant flow plus enable-persistence
  prerequisites for a 4-skill subset through the real gateway wrapper:
  Ouroboros's own skill review on ONE cheap stochastic reviewer slot
  (`google/gemini-3.5-flash`, low effort, `blocking` enforcement — pinned by
  the test's env; production reviewer defaults stay untouched), with
  auto-grant inside `review_skill`, then post-review dependency reconcile,
  enabled persistence (`save_enabled` + the toggle-gate facts — deliberately
  NOT the lifecycle toggle with `reconcile_extension`, which is server
  runtime and would execute downloaded plugin code in the secret-bearing
  process), and `skill_readiness_for_execution`. The
  CI job runs Tier 6 as a SEPARATE pytest step (fresh process) that alone
  carries `OPENROUTER_API_KEY`, ORDERED FIRST — the other tiers import
  downloaded plugin code in-process and must never share a process with the
  secret, and running the secret step first means the runner has never
  executed payload code while the secret was present — and only on the
  ubuntu shard (an LLM verdict is OS-independent). Paid step (~$1.2/run,
  ~$2.4 with the single fresh verdict retry); a missing key is a hard red,
  not a skip.

When adding a new opt-in lane, register the marker in `pyproject.toml`, add
a collect-only zero-test guard in CI, and keep the default local addopts
token-safe and Docker-safe.

### Parallel CI and the `serial` marker

CI runs the full default suite **in parallel** — `pytest -m "not serial" -n auto --dist loadscope
--max-worker-restart=0` (~5× faster than serial) — followed by a short serial pass for `-m serial`
(`.github/workflows/ci.yml`, jobs `quick-test` / `full-test`). Two rules keep new tests from breaking
that:

- **Mark real-process / real-port / process-global tests `@pytest.mark.serial`.** A test that spawns
  a real OS process, binds a real port, or mutates a module-level registry is not parallel-safe:
  under `-n` it flakes on kill/reap or port-reclaim timing, or it crashes its worker — which (with
  `--max-worker-restart=0`) fails that worker's WHOLE co-located batch and shows up as spurious
  failures in unrelated files. Mark such a test `@pytest.mark.serial` (or add its file to
  `_SERIAL_TEST_FILES` in `tests/conftest.py`) so it runs in the serial pass instead.
- **Keep every other test parallel-safe** so it stays in the fast pass: use `tmp_path` (never a fixed
  path like `/tmp/foo.pid`); use `monkeypatch.setenv` / `monkeypatch.setattr` (never a bare
  `os.environ[...] = ...`, which leaks to other tests on the same worker); never assume execution
  order; and if you must mutate a module global, reset it around the test (pattern:
  `tests/conftest.py::_isolate_workspace_executor_globals`).

### The commit gate mirrors the CI split (v6.88.0)

`ouroboros/preflight_runner.py::run_hermetic_pytest` no longer runs one serial pass. It runs
**the same two passes CI runs**, in the SAME disposable worktree and the same scrubbed env:

| Pass | markexpr | extra flags |
|------|----------|-------------|
| 1 `parallel` | `not serial and <LANE_EXCLUSION_EXPR>` | `-n auto --dist loadscope --max-worker-restart=0 --timeout=300 --timeout-method=thread` |
| 2 `serial`   | `serial and <LANE_EXCLUSION_EXPR>`     | none (flag-free, exactly like CI) |

> **Provisioning prerequisite — read this before pulling v6.88.0.** Before v6.88.0 the gate ran a
> plain serial pytest, so `pytest-xdist` was a CI-only dependency and a machine without it worked
> fine. From v6.88.0 the gate spawns pytest under the interpreter at `OUROBOROS_AGENT_PYTHON` (or
> `sys.executable`), and that interpreter MUST carry `pytest-xdist>=3.5` and `pytest-timeout>=2.1`.
> If it does not, the gate fails closed: **every** commit returns `PREFLIGHT_PLUGIN_MISSING` instead
> of running the suite. That is the designed behaviour — a degraded gate is indistinguishable from a
> passing one — but it means provisioning is a hard prerequisite, not a nicety. Install with
> `"$OUROBOROS_AGENT_PYTHON" -m pip install -r requirements.txt`, or set
> `OUROBOROS_PREFLIGHT_SERIAL=1` to fall back to the legacy single serial pass while you do.
>
> The same prerequisite reaches `tests/test_preflight_runner.py`, whose real-spawn tests run a
> NESTED pytest under `sys.executable`. It probes the interpreter once at import through the gate's
> own `_verify_preflight_plugins` and, if the plugins are absent, **skips only those tests** with a
> reason naming what to install — the fail-closed behaviour itself is pinned by hermetic and stubbed
> tests that never skip. Skips there mean "this interpreter cannot host a real pass", not "the
> property is unpinned"; CI installs both plugins, so that lane executes on every push.
>
> **Set `OUROBOROS_PREFLIGHT_REQUIRE_PLUGINS=1` whenever you are treating a run as evidence.**
> The skip is otherwise able to conceal itself: `requires_preflight_plugins` is keyed on the probe
> result, so a control test carrying that same marker skipped in exactly the case its assertion was
> written to catch, and an unprovisioned interpreter produced a fast, clean-looking run in which the
> only behavioural proofs of the forced-plugin and worker-probe machinery
> (`test_the_parallel_pass_really_starts_more_than_one_worker`,
> `test_a_candidate_cannot_switch_the_parallel_plugins_off`,
> `test_a_candidate_faking_the_parallel_flags_cannot_earn_a_green_pass`,
> `test_a_green_pass_cannot_leak_a_child_into_the_next_pass`) never executed. With the flag set,
> `test_plugin_verification_passes_on_the_interpreter_running_this_suite` hard-fails and names the
> missing distributions. `quick-test` and `full-test` set it at job level, and a review/repair gate
> command that is meant to prove this file should export it too.

Rules that keep this a fail-closed gate rather than a flake generator:

- **`LANE_EXCLUSION_EXPR` is the SSOT** for the costly marker lanes. A command-line `-m`
  **replaces** the `pyproject.toml` `addopts` `-m` entirely, so every pass restates the full
  conjunction. `test_lane_expr_matches_pyproject` pins it against `pyproject.toml`, and
  `test_each_ci_job_runs_the_same_split_the_gate_runs` pins BOTH markexprs and the exact
  `PARALLEL_PASS_FLAGS` vector against `quick-test` and `full-test` **separately** — a search of the
  whole workflow file stays green while either job alone drifts, and drift there silently re-admits
  an excluded lane into the gate.
- **The parallel lane must really be parallel.** `-n auto` resolves through
  `PYTEST_XDIST_AUTO_NUM_WORKERS`, so an inherited `1` runs the whole pass on one worker while the
  argv still reads `-n`, `PreflightPass.parallel` stays True, and the green return proves nothing
  about the parallel-only defects the pass exists to catch. `_preflight_env` therefore scrubs the
  **entire `PYTEST_*` namespace** — wholesale, not by name, because `PYTEST_ADDOPTS` (`-p no:xdist`,
  its own `-m`), `PYTEST_PLUGINS`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD` and the leaked
  `PYTEST_XDIST_WORKER`/`PYTEST_CURRENT_TEST` identities all weaken the pass the same way — and
  re-injects a worker count clamped to at least two. `OUROBOROS_PREFLIGHT_TEST_WORKERS` is a private
  test-only seam that can only lower the count TO that floor, and it is itself scrubbed before the
  candidate runs.
- **Plugin presence is verified independently of the candidate.** A candidate `conftest.py` can
  declare `-n`/`--dist`/`--timeout` with `pytest_addoption` and ignore them, so with xdist absent the
  nominal parallel lane runs serially, exits 0, and returns GREEN — "pytest did not reject the flags"
  is not evidence. `_verify_preflight_plugins` imports and version-checks xdist and pytest-timeout
  with the selected interpreter, from the disposable temp root **before the worktree is created**, so
  no candidate conftest, `sitecustomize`, or plugin can run before the answer is known. The strict
  probe uses `-I`; because `-I` also drops `PYTHONPATH` and the user site directory (neither of which
  is candidate-controlled), a strict miss is re-confirmed without them rather than hard-blocking a
  legitimate `pip install --user`.
- **A candidate that deletes the whole suite is a hard block.** Git does not track empty directories,
  so staging the removal of every test file removes `tests/` with them; the live-path check then
  returned success before the worktree existed, and the all-passes-empty invariant — reachable only
  *through* the passes — never ran. `_head_tracks_tests` separates "this repository has no suite"
  (out of scope) from "this change deleted the gate" (blocked) against the caller's **phase
  baseline**, because the two entry points see different repository states and neither baseline is
  right for both. The review preflight runs pre-commit (deletion merely staged, `HEAD` still carries
  the suite) and passes `phase=PRE_COMMIT_PHASE`, so it compares **`HEAD` only** — an `any()` over
  `HEAD~1` too rejected the first unrelated staged change after a deliberate removal commit, and that
  horizon expires only once the next commit exists, i.e. after the pre-commit gate has already
  refused to let it be made. The commit gate runs POST-commit — `_post_commit_result` is reached only
  once `commit_sha` exists, which is why its failure text says the commit "was already created and
  preserved" — so by then the deletion is *in* `HEAD`, a HEAD-only baseline answered "this repository
  has no test suite", and the block was unreachable from the entry point that most needed it; that
  phase therefore compares **`HEAD` and `HEAD~1`**. Exactly one commit back, so a project that
  genuinely dropped its tests is back out of scope on its next commit rather than blocked forever.
- **A red post-commit gate stops publication, not just the result text.** `_post_commit_result`
  returns the blocking error instead of only stashing a warning, and `_repo_commit_push` returns it
  before `_auto_push` — otherwise every hard block above (missing plugin, lost parallelism, crashed
  worker, deleted suite, failed containment) was pushed to origin behind an `OK:` result. The commit
  itself stays preserved; an auto-created version tag stays local. **A managed update is the one
  case where returning the block alone is not enough**: its transaction is written as
  `committing_assisted` *before* the 2-parent merge commit, and that phase means "died mid-commit",
  so boot recovery would promote the rejected merge to `pending_boot_smoke` and finalize it without
  ever rerunning the gate. `_repo_commit_push` therefore routes a red gate inside a managed tx into
  `rollback_managed_update` — the rejected merge is preserved on a `failed-update-*` branch, the
  tree is reset to `pre_update_sha`, and the tx marker is cleared so nothing can promote it. That
  forensics ref is written first but is deliberately BEST-EFFORT: getting the machine back onto a
  working revision is this function's actual job, and every caller except this gate (the post-boot
  smoke rollback, the gateway control paths) ignores the returned boolean and relies on the reset
  having happened. Aborting because a branch name could not be written — a ref/directory collision,
  a read-only refs dir — left the box running the bad update *and* returned before
  `_finalize_pending_boot_smoke` persisted `boot_attempts`, so the next boot repeated the same
  failing attempt forever. The lost ref is logged and named in the returned message instead.
  The gate is not the only return that reaches that state: BOTH `_verify_reviewed_commit_binding`
  mismatches (`review_binding_mismatch`, `review_tag_binding_mismatch`) abandon the commit after
  the same `committing_assisted` write, so all three route through the shared managed-failure
  helpers (`_review_binding_failure` / `_managed_post_commit_tests_gate` →
  `_managed_commit_gate_failure`). And because *clearing the marker is the last thing the rollback
  does*, a rollback that FAILS leaves the exact phase it was called to escape — so every
  unsuccessful rollback re-phases the tx to `gate_blocked` (`mark_update_tx_gate_blocked`), whose
  boot branch in `finalize_managed_update_on_boot` RETRIES the rollback rather than resuming or
  promoting the refused merge. "Unsuccessful" includes RAISED, and the re-phase runs in its own
  `try` for exactly that reason: the rollback executes several git commands before it clears the
  marker, so an exception halfway through is the case that most needs pinning, and an attempt
  nested inside the rollback's own `try` would be the one case that skipped it. The returned text
  reports what actually happened — it claims the tx is pinned only when that write returned true,
  and otherwise says the marker could not be re-phased and must be cleared before the next boot.
- **A detected leak is itself a hard block.** `_execute_pytest_pass` returns
  `(returncode, output, containment_error)` and reaps *before* returning, because a `finally:` block
  cannot alter an already-computed tuple — a scan whose verdict no caller can see is exactly the
  fail-open the container exists to close. A non-empty reason (a member still alive after the
  best-effort sweep, a member whose environment could not be read, a process table that would not
  enumerate, a Windows Job Object that could not be created or whose termination could not be
  confirmed) blocks the pass ahead of its exit code, green included, as
  `PREFLIGHT_CONTAINMENT_FAILED` naming the leaked pids and the mark-it-`@pytest.mark.serial`
  remediation.
- **A crashed xdist worker is an explicit hard block, never a retry.** `_classify_pass_result`
  recognises xdist's own controller phrasing (`crashed while running`, `node down:`,
  `worker gwN crashed …`, `replacing crashed worker`, `maximum crashed workers reached`) and returns
  `PARALLEL_WORKER_CRASH` naming the mark-it-`@pytest.mark.serial` remediation. This is
  what removes the old objection that a parallel gate manufactures `TESTS_FAILED`
  indistinguishable from a real rejection: a crash now has its own name. Both error directions
  are bounded, and they are not symmetric: a **miss** degrades only the label (the nonzero exit
  still blocks), while a **false positive** would be lossy — so the matched lines are only ever a
  highlighted PREFIX in front of the full pytest output, never a replacement for it. Each pattern
  matches a COMPLETE controller line shape (the phrase together with the `gwN` id or the numeric
  operand xdist always prints) after terminal decoration is stripped, never a free substring: bare
  `node down:`, `crashed while running`, `replacing crashed worker` and `maximum crashed workers
  reached` all occur in ordinary assertion text and captured logs of tests that reason about worker
  pools. Every pattern IS `^`-anchored to the stripped line; the `-q` short-summary re-emission
  (`handle_crashitem` reports the crash as a TestReport longrepr) is matched by its own anchored
  `FAILED/ERROR`-prefixed pattern rather than by unanchoring the phrase.
- **A worker killed by the per-test timeout gets the OPPOSITE remediation.** `--timeout-method=thread`
  does not fail the slow test; it dumps stacks and `os._exit`s the worker, which reaches the
  controller wearing exactly the crash phrasing above. So `_crash_remediation` first looks for
  pytest-timeout's own banner (`+++ Timeout +++`, or — for the signal method — `Timeout >300.0s`
  only as pytest's own `Failed:` exception line, because the bare phrase is ordinary text a test can
  print or assert on and matching it against the whole pass output would INVERT the remediation for a
  genuine crash running alongside such a test) and, when it is present, says *make the test faster or
  split it* and explicitly says **not** to mark it
  `@pytest.mark.serial`. That matters because pass 2 is flag-free: it carries no per-test timeout,
  so obeying the generic instruction would relocate the hang into the only pass that cannot bound
  it, where it eats the whole remaining total budget and returns as a pass-2 timeout — the exact
  failure `--timeout=300` was added to prevent. The label and the hard block are identical either
  way; only the instruction line changes.
- **A missing plugin fails closed, never degrades to serial.** Either the pre-run interpreter
  verification above, or pytest exit 4 whose `unrecognized arguments:` list contains a WHOLE token
  from `PARALLEL_PASS_FLAGS`, becomes `PREFLIGHT_PLUGIN_MISSING` naming the interpreter.
  `requirements.txt` declares `pytest-xdist>=3.5` and `pytest-timeout>=2.1` — declaring them is not
  installing them, so a provisioning miss surfaces here rather than as a quietly weaker gate.
- **Installed is not loaded, so the parallel pass forces the plugins ON and then PROVES it ran
  parallel.** Ini `addopts` are PREPENDED to the gate's argv, so a candidate `pytest.ini` with
  `-p no:xdist -p no:timeout` (plus a `conftest.py` that declares and ignores `-n`/`--dist`/
  `--timeout`) yields a lane labelled parallel that runs strictly serially and exits 0. The pass
  appends `-p xdist -p timeout`; `consider_preparse` walks `-p` entries in order, so the later
  unblock wins. Use the ENTRY-POINT names, never module paths: pytest skips an entry point whose
  name is already registered, whereas `-p xdist.plugin` registers the same module under a second
  name and pluggy raises `Plugin already registered`, failing runs that were green. The proof is
  independent of anything the candidate can print — the gate writes its own tiny plugin onto the
  run's `PYTHONPATH`, loads it with `-p`, and each xdist worker drops a file named for its
  `PYTEST_XDIST_WORKER` id; a GREEN pass reporting fewer than `_MIN_PREFLIGHT_WORKERS` distinct
  workers is `PREFLIGHT_PARALLELISM_LOST`, a hard block whose remediation is
  `OUROBOROS_PREFLIGHT_SERIAL=1` if a serial run is what you actually want. This closes the silent
  downgrade, not active forgery of the worker files. The check keys on the probe being present in
  the argv (NOT on `PreflightPass.parallel`), because an explicit caller `pytest_args` carrying its
  own `-n` is forwarded verbatim, never gets the probe, and must not be blocked for evidence the
  gate never asked for.
- **Both hard-block labels are gated on `PreflightPass.parallel`**, read off the argv rather than
  the label. A pass that never handed the run to xdist has no worker to crash and no xdist plugin
  to miss, so firing either label there attaches a remediation that is wrong by construction:
  telling a test already in the serial lane to mark itself `serial`, or blaming pytest-xdist for an
  unrelated usage error. Matching is whole-token for the same reason — `-n` is a substring of
  `--no-header`, which `DEFAULT_PYTEST_ARGS` passes on EVERY invocation. This is a message-quality
  rule, not a verdict rule: a nonzero exit blocks either way.
- **A diagnosis puts its remediation ahead of the pytest body.** `review_helpers` re-truncates the
  returned string from the TAIL at the same 8000-char limit, so a remediation printed after a
  full-budget body is the first thing lost — exactly when the output is long enough to need it.
  `_diagnosis` also reserves the header/remediation length out of the body budget, so the result
  stays inside the caller's declared limit instead of overrunning it — unconditionally, including
  when the limit is too small to hold the prefix itself. `_with_timeout_excerpt` obeys the same
  invariant without exception: when the message alone already fills the budget it is cut rather than
  returned whole, because a declared limit that holds on some branches and not others is not a limit.
- **The exit code decides the verdict; the rendered text only decides how it reads.** Because
  `_diagnosis` renders *inside* the caller's budget, a non-positive `max_output` produced an empty
  string for a real failure — and an empty diagnosis read as "no failure", turning a red pass into a
  green gate. A non-positive budget is now rejected before anything runs, and the pass loop blocks on
  the nonzero exit itself with a plain header as the fallback text.
- **Process containment is unconditional, not a timeout path.** `communicate()` returning proves only
  that the pytest CONTROLLER exited; a child a test spawned and never waited on survives it, and once
  the controller is gone that child is invisible to both the `pgrep -P` parent→child walk (the ppid
  links died with the parent) and the temp-root command-line sweep (its argv names no sweepable
  path). `process_containment.ProcessContainer` *spawns* pytest (`container.spawn`, not `Popen` + `adopt`)
  and is reaped in `finally` after EVERY pass, green included, which is what actually keeps pass 1
  out of pass 2 and off the machine. Spawning through the container is what closes the Windows
  assignment race: a Job Object holds only what has been assigned to it, so anything the process
  starts between `Popen` returning and the assignment is not a member and survives terminate/close —
  the process is therefore created `CREATE_SUSPENDED` and resumed only once it is in the job, the
  same sequence `launcher.py` uses for the agent server, through the same
  `create_kill_on_close_job`/`assign_pid_to_job`/`terminate_job`/`close_job` helpers.
  **The POSIX contract is DETECTION, not guaranteed teardown**, and that distinction is the design
  rather than a caveat. Guaranteed teardown of a detached foreign process is not something POSIX
  offers: a pid is a reusable name, membership is not kernel-held, and a signal can be refused
  (`EPERM`) or land on a recycled stranger — so every earlier attempt to promise reaping produced
  another PID-reuse edge instead of a guarantee. What `reap` can do honestly is LOOK. Membership is
  a unique **token planted in the root's environment**: the kernel copies the environment into every
  child at `fork` and preserves it across `exec`, and neither `setsid()`, nor closing every
  inherited descriptor, nor being reparented to init removes it, so `reap` resolves members from
  live kernel state (`pids_with_env_marker`, reading `/proc/<pid>/environ` on Linux and `ps -E`
  elsewhere) at the moment it runs — seeded with the pid `spawn` started, which is a member by
  construction and therefore the one member that does not have to be readable to be known.
  The token is claimed by READING the process, though, which leaves it two blind spots: a member
  that turns nondumpable or changes credentials before the FIRST scan is in no list at all (the
  seed only names the root, and "once seen, always watched" only holds pids it managed to see
  once), and a child spawned with a wholly REPLACED environment — an ordinary `Popen(env={...})`
  in a test — never carried the token to begin with. The root's **process group** covers both,
  because it is kernel-held and readable from outside no matter what the process did to its own
  environment or credentials. It is an ENUMERATION and CLASSIFICATION input **only**: a pid found
  by group alone is reported as alive and is never signalled. That asymmetry is the whole licence
  for reintroducing the group after PID-reuse bugs got it removed — reading a stale pgid can only
  ever ADD a pid to the leak report, costing a false BLOCK an operator can clear, whereas
  signalling one kills a bystander and no rescan undoes that. `spawn` also takes the group only
  when `start_new_session` made the root its own LEADER (`pgid == pid`); any other pgid is a group
  we did not create, and enumerating it would report the caller's own neighbours as leaks. It then
  attempts **ONE** bounded best-effort kill sweep — each signal aimed at a pid revalidated as a
  member microseconds earlier, with no lookup in between, so a recycled pid is never the target —
  and everything after the sweep is scan-only. A member is thus signalled at most once instead of
  re-signalled every 50ms for ten seconds: the revalidate-then-signal race cannot be closed, only
  entered as few times as possible, and a member that survived the first `SIGKILL` is one we cannot
  kill, so the block is already earned. Any member still alive, or any member whose liveness could
  not be DETERMINED, is returned as a failure naming the pids, and the gate hard-
  blocks. Cannot-determine is treated exactly as leaked: `pid_marker_state` is tri-state
  (`member` / `absent` / `unreadable`), because folding a `PermissionError` on a member's `environ`
  into "not a member" is how a live descendant leaves containment without exiting. Correctness
  therefore rests on the scan being honest and never on the kill having worked, which is why there
  is no `killpg` in the reap path at all — an emptied pgid is free for reuse, and signalling one
  from a snapshot is how a bystander gets SIGKILLed. When the deadline expires the report is built
  from the last NON-EMPTY scan rather than the current one, because the remediation tells the
  operator to kill the pids listed and a scan that happens to come back empty as the clock runs out
  would list none. Note the environmental prerequisite this
  places on POSIX systems **without** `/proc` (macOS, the BSDs): membership is read with `ps -E
  -ww`, and `ps` reports a process whose environment it may not print by simply *omitting* the
  environment — output identical to a process that never carried the token. That branch therefore
  fails closed on BOTH readings: only a non-zero `ps -p` exit (no such process) answers `absent`,
  while a pid that is alive and shows no token answers `unreadable` and blocks. The cost is that a
  pid recycled during a reap blocks the gate; the benefit is that a member turning nondumpable
  cannot leave quietly where there is no `/proc`. An image whose `ps` cannot run at all is the same
  block. Linux needs no `ps` for containment; the kernel exposes everything through `/proc`.
  This **replaced** a background `(pid, start_time)` poller, and the difference is a real leak, not
  a tuning question. The poller could only fingerprint a descendant while its ppid link still
  existed, so a child born AND orphaned between two 0.5s samples escaped both mechanisms — the
  fastest and most ordinary daemonising shape — and no sampling rate closes that. Resolving
  membership at reap time means there is no window to be born inside; the regression
  (`test_process_container_kills_a_descendant_that_left_the_group`) therefore spawns and exits with
  no sleep at all, where it previously had to sleep two seconds to accommodate the sampler.
  The remaining limit is narrower than either signal alone: a descendant escapes only by scrubbing
  its environment **and** calling `setsid()` to leave the group — two deliberate acts by code
  running with our own privileges, not an ordinary leak. The token lives OUTSIDE the
  `OUROBOROS_*` namespace `_preflight_env` scrubs, so a nested preflight's env keeps the outer
  container's token and the tokens compose instead of overwriting.
  Windows keeps its `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` Job Object, which is kernel-held membership
  outright — the one platform where teardown really is enforced, but only when the API says so:
  `terminate_job`/`close_job` return a reason instead of `None`, a FALSE Win32 BOOL counts as a
  failure exactly like a raised call, and `reap` performs both (terminate, then close for the
  kill-on-close backstop) so either failure reaches the verdict rather than the discarded handle.
- **A timed-out pass reports what it had already flushed.** The post-kill `communicate` collects the
  child's output; discarding it left the operator with "the serial pass timed out" and nothing naming
  the test that hung — and pass 2 is exactly the pass with no per-test timeout to name it otherwise.
  The excerpt keeps the TAIL and shares the same `max_output` budget.
- **An explicitly EMPTY `pytest_args` maps to `DEFAULT_PYTEST_ARGS`, not to a literally empty argv.**
  The two-pass default is reserved for `pytest_args is None` (silently upgrading an explicit argv
  would run tests the caller never asked for under xdist requirements they never opted into), but an
  empty sequence must keep the pre-two-pass runner's truthiness behaviour: forwarding no argv at all
  changes the discovery target and drops the output flags.
- **Exit 5 is green PER PASS, blocking only when EVERY pass is empty.** A tiny candidate repo with
  zero `serial` tests must not be false-blocked; a `tests/` tree with no runnable test anywhere still is.
- **One TOTAL budget** (900s, `OUROBOROS_PREFLIGHT_TIMEOUT_SEC`). Pass 2 gets exactly `total −
  elapsed` as a float — never clamped up to a whole second and never rounded, or the gate would
  outlive the total it advertises; an already-exhausted budget returns a pass-named error WITHOUT
  spawning pass 2 at all. The header and the timeout message report each pass's OWN duration, so a
  fast serial pass is never blamed for the wall-clock a slow parallel pass burned. CI's two-pass split completes the same
  suite in ~178s against the ~466–510s of a single serial run, so the budget gains headroom rather
  than losing it. That figure is CI-measured; reproduce it locally with a real
  `run_hermetic_pytest` call before quoting it as a local number.
- **Fail-fast**: the first red pass returns immediately with that single pass's output, so the
  failing section can never be truncated away by merging a second pass into the same 8000-char budget.
- **`OUROBOROS_PREFLIGHT_SERIAL=1`** forces the legacy single serial pass — an instant operator
  rollback lever. The variable is scrubbed by `_preflight_env`, so the candidate suite cannot see it.

Wall-clock shape: the serial lane is roughly 20% of the total, with a ~72–80s `loadscope` floor set
by `tests/test_osworld_cu_bridge.py` (moving to `--dist load` is out of scope — it would break the
per-file fixture locality the serial split assumes).

**Pre-switch audit (v6.88.0):** four consecutive full-suite runs under the two-pass split were
byte-identical to the serial baseline with zero worker crashes. Ongoing protection is (a) the static
serial-candidate checklist below and (b) the in-gate crash=hard-block canary, which self-reports
drift at the first offending commit rather than silently flaking.

**Static checklist when adding a test** (syntax cannot prove mocking, so the semantic call in
`docs/CHECKLISTS.md` item 18 stays mandatory) — grep the new test for:
`subprocess.Popen` / `subprocess.run`, socket or fixed-port binding, `start_new_session=True`,
and un-monkeypatched module globals. Any hit means `@pytest.mark.serial`.

### GitHub Actions: secrets in step-level `if:` conditions

GitHub Actions rejects `secrets.*` inside step-level `if:` expressions, and a
step's own `env:` block is not visible to that same step's `if:`. Derive a
non-secret boolean in the job-level `env:` block, gate steps with that boolean,
and map the actual credentials only inside the first-party steps that need them.

```yaml
jobs:
  build:
    runs-on: macos-latest
    env:
      HAS_APPLE_SIGNING: ${{ secrets.BUILD_CERTIFICATE_BASE64 != '' && secrets.P12_PASSWORD != '' && 'true' || 'false' }}
    steps:
      - name: Import Apple signing certificate
        if: env.HAS_APPLE_SIGNING == 'true'
        env:
          BUILD_CERTIFICATE_BASE64: ${{ secrets.BUILD_CERTIFICATE_BASE64 }}
          P12_PASSWORD: ${{ secrets.P12_PASSWORD }}
        run: |
          echo "${BUILD_CERTIFICATE_BASE64}" | base64 -d > cert.p12
          security import cert.p12 -P "${P12_PASSWORD}" ...
      - name: Cleanup keychain
        if: always() && env.HAS_APPLE_SIGNING == 'true'
        run: security delete-keychain ...
```

```yaml
# ❌ WRONG — workflow fails to parse
- name: Bad
  if: secrets.BUILD_CERTIFICATE_BASE64 != ''   # parse error
  env:                                          # not visible to this step's if:
    P12_PASSWORD: ${{ secrets.P12_PASSWORD }}
```

`tests/test_build_scripts.py::TestMacOSSigning::test_ci_uses_env_context_for_condition`
enforces this across every workflow `if:` block.

### Apple signing & notarization (macOS Build job)

When Apple signing secrets are configured, the macOS shard imports the Developer
ID certificate into a temporary keychain and `build.sh` signs the `.app` and
`.dmg` via `SIGN_IDENTITY`. Only a non-secret `HAS_APPLE_SIGNING` gate is
job-wide. Certificate and keychain values exist only in the import step, while
Apple ID notarization values exist only in the first-party build step. Later
SBOM and attestation steps inherit none of them. If `APPLE_ID` and
`APPLE_APP_SPECIFIC_PASSWORD` are present, notarization runs; otherwise the DMG
ships signed but not notarized. Notary/stapler failures are soft warnings,
recorded through `NOTARIZE_OUTCOME`, so transient Apple issues do not silently
drop the macOS artifact. Cleanup uses `always()` plus macOS/env guards, and
signing material never persists across runs.

### Release proof capsule

The tagged build binds public release assets to their source and verification
record. Each platform shard locates the final DMG, tarball, or ZIP after all
packaging steps, then performs a smoke test against that final archive. The
smoke checks require the embedded repository bundle, run the packaged CLI with
`--help` in an isolated home directory, then use the embedded Claudexor seed and
Node from that extracted final artifact to perform install, extraction, exact
identity probe, owned-daemon handshake, one fake task, and an identity-bound
graceful stop of the serving closure. The separate
Claudexor platform gate repeats that fixture path on ordinary branch changes and
adds the explicit-key live compatibility matrix; neither path installs a
floating Claudexor npm package. The macOS check also requires the
`Applications -> /Applications` drag target, the separate `Install CLI.command`
payload, and an arm64 app executable.

Each shard also generates a CycloneDX SBOM from the payload extracted from the
final archive. The macOS smoke proves the Applications link, then removes only
that link from the SBOM staging copy so Syft cannot follow it into the runner's
host `/Applications`; the app and CLI launcher remain in the scan. The workflow
downloads a fixed Syft release asset and checks its platform-specific SHA-256
before execution. GitHub artifact attestations bind both build provenance and
the SBOM to the final archive digest. The release job downloads the three
archives and their proof files, checks the exact platform allowlist,
recalculates every digest, and verifies both predicates against the exact source
SHA, tag ref, repository, and signer workflow before it writes:

- `SHA256SUMS` for archives, SBOMs, and smoke receipts;
- `release-evidence.json` with tag, commit, workflow, checks, and artifact
  bindings;
- release notes from the matching README Version History row.

Publication uses a draft release. A per-tag concurrency group serializes release
jobs, and a fail-closed preflight allows only an absent release or an existing
draft; a published release is never overwritten by a rerun. The workflow
uploads only the explicit allowlist and compares GitHub's stored sizes and
SHA-256 digests with the local files. Immediately before draft creation and
again before publication, it requires the remote tag to exist as an annotated
tag whose peeled commit is the workflow event SHA. It publishes only after all
of those checks pass. A release from an
older workflow may receive a clearly labelled post-publication checksum
inventory, but it must never claim build-time provenance, an SBOM, or packaged
smoke evidence that the original build did not create.
