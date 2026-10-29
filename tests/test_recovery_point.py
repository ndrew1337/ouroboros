"""The local recovery point follows the commits this installation proved, and a fallback to it goes
through the store-switch owner (``supervisor/git_ops_reset.py``). Real Git in tmp.

A ready boot moves ``ouroboros-stable`` (``update-ref``, local, never pushed) to the commit whose import
smoke passed in that process on a clean, launcher-managed dev checkout, an update being finalized
included; never live HEAD. A start whose dev branch fails its smoke never boots a recovery point that
cannot read this installation's stores: it stops with a typed failure; one that can gets the stores
handed off and is checked out as before, the dev branch untouched.
"""
from __future__ import annotations

import subprocess

import pytest

from supervisor import git_ops, git_ops_reset
from tests._git_ops_recovery_shared import _git
from tests.test_git_bootstrap_receipt import bootstrap  # noqa: F401 - the shared real-Git fixture

STORES = ("ouroboros/store.py", "ouroboros/usage_store.py", "ouroboros/evidence_packs.py", "ouroboros/dialogue_store.py")


@pytest.fixture
def managed(bootstrap, monkeypatch):  # noqa: F811
    repo, drive, calls = bootstrap
    monkeypatch.setenv("OUROBOROS_MANAGED_BY_LAUNCHER", "1")
    monkeypatch.setattr(git_ops_reset, "_SMOKED", {})  # one process: one generation
    first = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    _git(repo, "branch", "ouroboros-stable", first)
    return repo, drive, calls, first


def _stable(repo) -> str:
    return _git(repo, "rev-parse", "ouroboros-stable")


def test_a_ready_boot_moves_the_recovery_point_to_the_commit_its_smoke_proved(managed):
    repo, _drive, calls, first = managed
    head = _git(repo, "rev-parse", "HEAD")
    assert git_ops.safe_restart(reason="bootstrap")[0] and "smoke" in calls
    assert git_ops.advance_recovery_point(False) == "" and _stable(repo) == first, "not ready: nothing moves"
    assert git_ops.advance_recovery_point(True) == head and _stable(repo) == head, "the old boot left July's point"
    assert git_ops.advance_recovery_point(True) == "", "already there"
    (repo / "value.txt").write_text("a later commit nobody smoked\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "later")
    assert git_ops.advance_recovery_point(True) == "" and _stable(repo) == head, "never live HEAD"


def test_an_update_being_finalized_counts_though_its_receipt_stays_off(managed):
    repo, drive, _calls, _first = managed
    (repo / ".git" / "ouroboros-update-tx.json").write_text("{}", encoding="utf-8")
    assert git_ops.import_test()["ok"] and not (drive / "state" / "git_bootstrap.json").exists()
    assert git_ops.advance_recovery_point(True) == _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("case", ["dirty", "other_branch", "unmanaged", "no_smoke_this_process"])
def test_only_a_clean_managed_dev_smoke_moves_it(managed, monkeypatch, case):
    repo, _drive, _calls, first = managed
    if case == "dirty":
        (repo / "untracked.txt").write_text("owner work\n", encoding="utf-8")
    elif case == "other_branch":
        _git(repo, "checkout", "-q", "-b", "experiment")
    elif case == "unmanaged":
        monkeypatch.delenv("OUROBOROS_MANAGED_BY_LAUNCHER")
        monkeypatch.setattr(git_ops, "_read_managed_repo_meta", lambda: {})
    if case != "no_smoke_this_process":
        assert git_ops.import_test()["ok"]
    assert git_ops.advance_recovery_point(True) == "" and _stable(repo) == first


def _failing_dev_smoke(monkeypatch, repo, dev_head):
    run = subprocess.run

    def smoke(cmd, **kwargs):
        if len(cmd) == 3 and cmd[1] == "-c" and cmd[2].startswith("import ouroboros,"):
            broken = run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip() == dev_head
            return subprocess.CompletedProcess(cmd, int(broken), "" if broken else "import_ok", "broken dev" if broken else "")
        return run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", smoke)


def test_a_recovery_point_that_cannot_read_the_stores_is_never_started(managed, monkeypatch):
    repo, _drive, _calls, first = managed
    dev_head = _git(repo, "rev-parse", "HEAD")
    _failing_dev_smoke(monkeypatch, repo, dev_head)
    ok, message = git_ops.safe_restart(reason="bootstrap")
    assert not ok and message.startswith("recovery_point_predates_stores:"), message
    assert dev_head[:12] in message and first[:12] in message and "use Restore on the Updates page" in message
    assert _git(repo, "rev-parse", "HEAD") == dev_head, "the old fallback checked July's code out over today's stores"
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "ouroboros"


def test_a_recovery_point_that_reads_the_stores_is_handed_off_and_checked_out_dev_untouched(managed, monkeypatch):
    repo, _drive, _calls, _first = managed
    dev_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "ouroboros-stable")
    for rel in STORES:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("# a store-aware release\n", encoding="utf-8")
    _git(repo, "add", "ouroboros")
    _git(repo, "commit", "-qm", "store-aware recovery point")
    stable = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "ouroboros")
    handed = []
    monkeypatch.setattr("ouroboros.store_switch._hand_off_stores", lambda root: handed.append(root) or "")
    _failing_dev_smoke(monkeypatch, repo, dev_head)
    assert git_ops.safe_restart(reason="bootstrap") == (True, "OK: fell back to ouroboros-stable")
    assert handed, "the stores were handed off before the checkout"
    assert _git(repo, "rev-parse", "HEAD") == stable and _git(repo, "rev-parse", "ouroboros") == dev_head
