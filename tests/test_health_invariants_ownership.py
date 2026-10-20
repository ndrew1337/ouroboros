"""Delegated-run health obligations follow ownership.

The obligations stay globally visible (a preserved-and-invisible result is how
work rots on disk), but only the OWNER task receives each run's line with its
call-shaped instruction: ``integrate_delegated_patch`` refuses a non-owner with
``run_not_owned`` while the owner task is LIVE, and the read acknowledgement
only credits the owner. Another task's runs fold into ONE line per owner task
(owner decision Q3=A, 18.10.2026: 223 such lines, ~110k characters, filled one
task's Health section), with the run ids and a run's exact reader, after the
authority rule (once the owner is terminal, reject from any root, apply from
the recorded target) stated once for all of them.
"""

import types

import ouroboros.context_health as context_health
from ouroboros.delegate_custody import RunCustody
from tests._governance_docs_shared import architecture_text


def _env(tmp_path):
    env = types.SimpleNamespace()
    env.drive_root = tmp_path
    env.repo_path = lambda *parts: tmp_path / "repo-none" / "/".join(parts)
    env.drive_path = lambda *parts: tmp_path / "/".join(parts)
    return env


def _foreign_runs(monkeypatch):
    unread = RunCustody(run_id="run-u", task_id="task-owner")
    unread.output_artifact = "delegated/run-u.out"
    patch = RunCustody(run_id="run-p", task_id="task-owner")
    patch.patch_captured = True
    patch.target_root = "/tmp/target"
    monkeypatch.setattr(
        context_health, "build_health_invariants", context_health.build_health_invariants
    )
    import ouroboros.delegate_custody as custody

    monkeypatch.setattr(
        custody, "settled_unread_outputs", lambda root, state=None: [unread])
    monkeypatch.setattr(
        custody, "undisposed_patches", lambda root, state=None: [patch])


_RULE = "Only the owner reads and acknowledges its results and decides its patches while it lives"


def _rule_line(text):
    """The authority rule all other-task lines share: ONE line, naming no owner of its own."""
    lines = [line for line in text.splitlines() if _RULE in line]
    assert len(lines) == 1 and "owner task " not in lines[0], lines
    return lines[0]


def test_non_owner_sees_one_line_per_owner_task_and_no_call_shaped_instruction(tmp_path, monkeypatch):
    _foreign_runs(monkeypatch)
    text = context_health.build_health_invariants(_env(tmp_path), task_id="task-other")
    rows = [line for line in text.splitlines() if "task-owner" in line]
    assert len(rows) == 1 and "ANOTHER TASK'S DELEGATED WORK — owner task task-owner" in rows[0]
    assert "results not read to EOF: run-u" in rows[0] and "patches awaiting apply/reject: run-p" in rows[0]
    assert "DELEGATED RESULT NEVER READ" not in text and "DELEGATED PATCH AWAITS DISPOSITION" not in text
    # The obligation is visible with its rule and reader; the DIRECTLY CALLABLE shape is not.
    assert "integrate_delegated_patch(run_id='run-p'" not in text
    rule = _rule_line(text)  # stated once, before the owner's line
    assert text.index(rule) < text.index(rows[0]) and _RULE not in rows[0]
    assert "once it is terminal, a live top-level task may reject them from any root" in rule
    assert "apply them from the recorded target" in rule and "disposition row records who acted" in rule
    assert "query_data(op='json', kind='task_result', ids=['task-owner'])" in rows[0]  # readable from any task
    assert "or a fresh payload binding" in rule
    assert "read_file" not in text


def test_each_owner_task_gets_one_line_and_own_runs_keep_theirs(tmp_path, monkeypatch):
    import ouroboros.delegate_custody as custody
    from ouroboros.delegate_custody import RunCustody

    def unread(run_id, task):
        run = RunCustody(run_id=run_id, task_id=task)
        run.output_artifact = f"delegated/{run_id}.out"
        return run

    runs = [unread(f"run-a{i}", "task-a") for i in range(12)] + [unread("run-b0", "task-b"), unread("run-own", "me")]
    monkeypatch.setattr(custody, "settled_unread_outputs", lambda root, state=None: runs)
    monkeypatch.setattr(custody, "undisposed_patches", lambda root, state=None: [])
    text = context_health.build_health_invariants(_env(tmp_path), task_id="me")
    folded = [line for line in text.splitlines() if "ANOTHER TASK'S DELEGATED WORK — owner task " in line]
    assert [line.split("owner task ")[1].split(":")[0] for line in folded] == ["task-a", "task-b"]
    assert all(f"run-a{i}" in folded[0] for i in range(12))
    assert "DELEGATED RESULT NEVER READ — run run-own" in text and "run-own" not in "".join(folded)
    assert _rule_line(text) and not any(_RULE in line for line in folded)  # two owners, one rule


def test_architecture_states_the_terminal_owner_apply_reject_authority_split():
    architecture = architecture_text()
    assert "PATCH_DISPOSED" in architecture
    assert "disposed_by_task_id" in architecture


def test_owner_keeps_the_call_shaped_instruction(tmp_path, monkeypatch):
    _foreign_runs(monkeypatch)
    text = context_health.build_health_invariants(_env(tmp_path), task_id="task-owner")
    assert "integrate_delegated_patch(run_id='run-p'" in text
    assert "read_file" in text


def test_unattributed_reader_keeps_the_call_shape(tmp_path, monkeypatch):
    """Background Consciousness and legacy callers pass no task id; they may
    be the owner, so the call-shaped wording survives."""
    _foreign_runs(monkeypatch)
    text = context_health.build_health_invariants(_env(tmp_path))
    assert "integrate_delegated_patch(run_id='run-p'" in text
    assert "read_file" in text


def test_one_custody_read_per_health_invariant_build(tmp_path, monkeypatch):
    """I18: the delegated-run lines share ONE custody read, and it is selected.

    Reading every custody row per block, and then per build, decoded the whole
    settled history each time; the shared read now selects the rows the lines
    can name (own, held, maybe-unread) and no full read runs unless it fails.
    SQL still visits every row, so the read stays O(rows) in the store.
    """
    import ouroboros.delegate_custody as custody
    import ouroboros.delegate_custody_current as current

    calls: list = []
    replays: list = []
    real_select, real_replay = current.health_runs, custody.replay
    monkeypatch.setattr(
        current, "health_runs",
        lambda root, task_id="": calls.append(root) or real_select(root, task_id),
    )
    monkeypatch.setattr(
        custody, "replay",
        lambda root, rows=None: replays.append(root) or real_replay(root, rows),
    )

    context_health.build_health_invariants(_env(tmp_path), task_id="task-owner")

    assert len(calls) == 1
    assert replays == []


# --- P6-3: the same obligation, for a reader whose own root already reaches it ---
#
# The recovery root's prompt carried sixteen near-identical abstract rows: the
# foreign branch keys on task identity alone, so it printed the rule even where
# the run's recorded target WAS that reader's own active root. `active_root` is
# threaded from the call site (the module has no ctx) and answered with the same
# predicate the apply gate asks, so the two can never disagree.


def _foreign_patch_targeting(monkeypatch, target_root):
    """One FOREIGN undisposed patch recorded against ``target_root``."""
    patch = RunCustody(run_id="run-p", task_id="task-owner")
    patch.patch_captured = True
    patch.target_root = str(target_root)
    import ouroboros.delegate_custody as custody

    monkeypatch.setattr(custody, "settled_unread_outputs", lambda root, *_a, **_k: [])
    monkeypatch.setattr(custody, "undisposed_patches", lambda root, *_a, **_k: [patch])


def _patch_row(text):
    return next(line for line in text.splitlines()
                if "ANOTHER TASK'S DELEGATED WORK" in line and "patches awaiting apply/reject" in line)


def test_reader_whose_active_root_is_the_target_gets_the_concrete_call(tmp_path, monkeypatch):
    target = tmp_path / "target"
    _foreign_patch_targeting(monkeypatch, target)
    text = context_health.build_health_invariants(
        _env(tmp_path), task_id="task-other", active_root=str(target))
    # The static rule still leads; the call FOLLOWS it.
    assert (text.index("apply them from the recorded target")
            < text.index("Your active root matches the target of run-p"))
    assert "integrate_delegated_patch(run_id='run-p', decision='apply'|'reject')" in text


def test_reader_whose_active_root_contains_a_host_minted_target_gets_the_call(tmp_path, monkeypatch):
    """The aggregator shape: the target is a clone NESTED in the reader's own
    root, both under the host-minted subagent-projects root."""
    projects_root = tmp_path / "projects"
    parent = projects_root / "P"
    target = parent / "contributions" / "tools"
    target.mkdir(parents=True)
    monkeypatch.setenv("OUROBOROS_SUBAGENT_PROJECTS_ROOT", str(projects_root))
    _foreign_patch_targeting(monkeypatch, target)
    text = context_health.build_health_invariants(
        _env(tmp_path), task_id="task-other", active_root=str(parent))
    assert "integrate_delegated_patch(run_id='run-p', decision='apply'|'reject')" in text


def test_an_unrelated_active_root_keeps_the_static_row_byte_for_byte(tmp_path, monkeypatch):
    target = tmp_path / "target"
    _foreign_patch_targeting(monkeypatch, target)
    static = _patch_row(context_health.build_health_invariants(
        _env(tmp_path), task_id="task-other"))
    unrelated = _patch_row(context_health.build_health_invariants(
        _env(tmp_path), task_id="task-other", active_root=str(tmp_path / "elsewhere")))
    assert unrelated == static
    assert "integrate_delegated_patch(run_id='run-p'" not in unrelated


# --- 20.10.2026: the rule once per block, each owner's facts on its own line ---
#
# Every other-task line repeated the same authority rule: 258 owners, 76k of one
# Health section's 128k characters. The rule now leads those lines once; each owner's
# line keeps its severity, owner, run ids by kind, reader and, where it applies, the call.


def test_the_rule_is_stated_once_for_one_owner_and_for_eighteen(tmp_path, monkeypatch):
    import ouroboros.delegate_custody as custody

    def health(owners):
        runs = [RunCustody(run_id=f"run-{owner}", task_id=owner) for owner in owners]
        for run in runs:
            run.output_artifact = f"delegated/{run.run_id}.out"
        monkeypatch.setattr(custody, "settled_unread_outputs", lambda root, state=None: runs)
        monkeypatch.setattr(custody, "undisposed_patches", lambda root, state=None: [])
        text = context_health.build_health_invariants(_env(tmp_path), task_id="me")
        rule = _rule_line(text)
        rows = [line for line in text.splitlines() if "ANOTHER TASK'S DELEGATED WORK — owner task " in line]
        assert len(rows) == len(owners) and not any(_RULE in row for row in rows)
        assert text.splitlines().index(rule) + 1 == text.splitlines().index(rows[0])
        return rows

    many = health([f"task-{i}" for i in range(18)])
    # A row carries only its own owner's facts: byte-identical among eighteen owners and alone.
    assert health(["task-0"]) == many[:1]
    assert many[17] == ("- INFO: ANOTHER TASK'S DELEGATED WORK — owner task task-17: results not read to EOF: "
                        "run-task-17. The owner's record: query_data(op='json', kind='task_result', ids=['task-17']).")


def _put_custody(root, rows):
    """Custody rows exactly as the reducer stores them: ``put_custody`` and its hold column."""
    from ouroboros import delegate_custody_current as current
    from ouroboros import store

    with store.open_store(root, write=True) as txn:
        for run_id, facts in rows:
            facts = {"run_id": run_id, **facts}
            hold = current.HELD if current._held(current._decode(facts)) else current.CLOSED
            txn.put_custody(run_id, facts, state=hold)


def test_a_real_ledger_keeps_every_fact_of_every_owner_under_one_rule(tmp_path):
    """The reader's own, another task's, terminal, capture-gap and active-target runs, read from custody."""
    target = tmp_path / "target"
    target.mkdir()
    settled = {"settled": True, "terminal_state": "failed", "settled_at": "2026-10-20T00:00:00Z"}
    unread = {**settled, "output_complete": True}
    _put_custody(tmp_path, [
        ("run-me-u", {"task_id": "me", **unread, "output_artifact": "me.out"}),
        ("run-me-p", {"task_id": "me", **settled, "snapshot_id": "s1", "target_root": "/tmp/t-me",
                      "patch_captured": True}),
        ("run-me-gap", {"task_id": "me", **settled, "snapshot_id": "s2", "target_root": "/tmp/t-gap"}),
        ("run-me-end", {"task_id": "me", **settled, "engine_locator": {"run_dir": "/engine/runs/me"}}),
        ("run-a1", {"task_id": "task-a", **unread, "output_artifact": "a1.out"}),
        ("run-a2", {"task_id": "task-a", **unread, "output_artifact": "a2.out"}),
        ("run-a-p", {"task_id": "task-a", **settled, "snapshot_id": "s3", "target_root": str(target),
                     "patch_captured": True}),
        ("run-b-gap", {"task_id": "task-b", **settled, "snapshot_id": "s4", "target_root": "/tmp/elsewhere"}),
        ("run-c1", {"task_id": "task-c", **unread, "output_artifact": "c1.out"}),
    ])
    text = context_health.build_health_invariants(_env(tmp_path), task_id="me", active_root=str(target),
                                                  attempt_key="1")
    lines = text.splitlines()
    rule = _rule_line(text)
    rows = {line.split("owner task ")[1].split(":")[0]: line for line in lines
            if "ANOTHER TASK'S DELEGATED WORK — owner task " in line}
    assert lines.index(rule) + 1 == min(lines.index(row) for row in rows.values())
    reader = "The owner's record: query_data(op='json', kind='task_result', ids=['{}'])."
    assert rows == {
        "task-a": ("- WARNING: ANOTHER TASK'S DELEGATED WORK — owner task task-a: results not read to EOF: run-a1, "
                   "run-a2; patches awaiting apply/reject: run-a-p. " + reader.format("task-a")
                   + " Your active root matches the target of run-a-p: once the owner is terminal, "
                   "integrate_delegated_patch(run_id='run-a-p', decision='apply'|'reject')."),
        "task-b": ("- WARNING: ANOTHER TASK'S DELEGATED WORK — owner task task-b: patches awaiting apply/reject: "
                   "run-b-gap. " + reader.format("task-b")),
        "task-c": ("- INFO: ANOTHER TASK'S DELEGATED WORK — owner task task-c: results not read to EOF: run-c1. "
                   + reader.format("task-c")),
    }
    own = [line for line in lines if "owner task me" in line or "run run-me-end" in line]
    assert [line.split(" — run ")[1].split()[0] for line in own] == [
        "run-me-u", "run-me-end", "run-me-gap", "run-me-p"]
    assert "settled with its work preserved in its private snapshot of /tmp/t-gap (no patch captured yet" in text
    assert "integrate_delegated_patch(run_id='run-me-p', decision='apply'|'reject')" in text and "read_file" in text
    assert not any(_RULE in line for line in own)
    # Nothing is another task's for an unattributed reader: every run keeps its own line, and no rule.
    unattributed = context_health.build_health_invariants(_env(tmp_path), active_root=str(target), attempt_key="1")
    assert _RULE not in unattributed and "ANOTHER TASK'S DELEGATED WORK" not in unattributed
    assert "DELEGATED PATCH AWAITS DISPOSITION — run run-a-p" in unattributed
