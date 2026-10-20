"""The deletion audit's three actual consumer regressions, with disposable state.

The worker process uses the ordinary local lifecycle runner; the server owns a
loaded provider's retained imports. Nothing here starts a vendor connection.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from tests.test_realtime_setup_consumers import local_provider  # noqa: F401
from tests.test_realtime_setup import _clear_loader_state  # noqa: F401


def _receipt(name, value):
    root = os.environ.get("OUROBOROS_TEST_EVIDENCE_ROOT")
    if root:
        path = Path(root) / "live-lifecycle"
        path.mkdir(parents=True, exist_ok=True)
        (path / (name + ".json")).write_text(json.dumps(value, indent=2, default=str))


def _delete_worker(drive, name, result):
    """Separate PID: it cannot see the server's _retiring_imports dictionary."""
    from ouroboros.skill_lifecycle_actions import run_skill_action
    from ouroboros.skill_loader import find_skill
    from ouroboros.tools.tool_context import ToolContext

    root = Path(drive)
    skill = find_skill(root, name, repo_path="")
    ctx = ToolContext(repo_dir=root / "synthetic-repo", drive_root=root, branch_dev="test",
                      task_id="synthetic-worker-delete", current_chat_id=1, is_direct_chat=True)
    try:
        value = run_skill_action(ctx, name, "delete", repo_path="",
            expected_content_hash=skill.content_hash, payload_root=f"skills/external/{name}",
            _owner_actor="owner_cli")  # synthetic host admission; not an authority test
        result.put(value)
    except BaseException as exc:
        result.put({"exception": type(exc).__name__, "message": str(exc)})


def test_worker_delete_respects_live_server_pid_reference(local_provider):
    from ouroboros import extension_loader as loader
    from ouroboros.extension_import_staging import _module_key
    from ouroboros.extension_registry_state import _extensions, extension_work_scope
    from ouroboros.realtime_setup import loaded_provider

    skill, drive = local_provider
    bundle = _extensions[skill.name]
    tree = Path(bundle.import_root)
    state = drive / "state/skills" / skill.name
    worker_context = multiprocessing.get_context("spawn")
    result = worker_context.Queue()
    worker = worker_context.Process(target=_delete_worker, args=(str(drive), skill.name, result))
    receipt = {"server_pid": os.getpid(), "tree": str(tree)}

    class Active:
        cancelled = False
        def cancel(self):
            self.cancelled = True

    active = Active()
    try:
        with extension_work_scope(loaded_provider("openai_realtime:openai"), active, retain_imports=True):
            worker.start()
            worker.join(20)
            assert not worker.is_alive(), "ordinary local lifecycle did not return"
            value = result.get(timeout=2)
            receipt.update(worker_pid=worker.pid, worker_exit=worker.exitcode, result=value)
            assert worker.exitcode == 0 and value["ok"], receipt
            assert value["state_delete_pending"] and not value["deleted_state"], receipt
            assert not skill.skill_dir.exists() and tree.exists()
            assert (tree / "owners" / str(os.getpid())).exists()
            # Server reconcile retires admission, but teardown still imports.
            loader.unload_extension(skill.name)
            assert active.cancelled
            receipt["late_import"] = importlib.import_module(_module_key(skill.name) + ".late_identity").VALUE
            assert receipt["late_import"] == "synthetic-active-import"
            assert state.exists()
        assert not state.exists()
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        receipt["state_remains"] = state.exists()
        _receipt("worker-server", receipt)
        result.close()


@pytest.mark.parametrize("bucket", ["native", "ouroboroshub"])
def test_cross_bucket_same_name_reinstall_preserves_new_state(local_provider, bucket):
    from ouroboros import extension_loader as loader
    from ouroboros.extension_registry_state import extension_work_scope, _retiring_imports
    from ouroboros.realtime_setup import loaded_provider
    from ouroboros.skill_uninstall_state import delete_local_skill, UNINSTALL_TOMBSTONE_FILENAME

    skill, drive = local_provider
    manifest = (skill.skill_dir / "SKILL.md").read_text()
    state = drive / "state/skills" / skill.name
    new = drive / "skills" / bucket / skill.name

    class Active:
        def cancel(self):
            pass

    try:
        with extension_work_scope(loaded_provider("openai_realtime:openai"), Active(), retain_imports=True):
            result = delete_local_skill(drive, skill)
            assert result["state_delete_pending"]
            assert not skill.skill_dir.exists()
            new.mkdir(parents=True)
            (new / "SKILL.md").write_text(manifest)
            (state / "grants.json").write_text('{"synthetic_new_grant":true}')
            (state / "new-state.json").write_text('{"replacement":true}')
        assert skill.name not in _retiring_imports
        assert (state / "grants.json").read_text() == '{"synthetic_new_grant":true}'
        assert (state / "new-state.json").exists()
        assert not (state / UNINSTALL_TOMBSTONE_FILENAME).exists()
    finally:
        _receipt("reinstall-" + bucket, {"old_payload_exists": skill.skill_dir.exists(),
            "new_payload_exists": new.exists(), "state_files": sorted(p.name for p in state.iterdir()) if state.exists() else []})
        loader.unload_extension(skill.name)


def test_deferred_cleanup_failure_retires_imports_and_retries(local_provider, monkeypatch):
    from ouroboros.extension_registry_state import extension_work_scope, _retiring_imports, realtime_load_refusal
    from ouroboros.realtime_setup import loaded_provider
    from ouroboros import skill_uninstall_state as cleanup

    skill, drive = local_provider
    state = drive / "state/skills" / skill.name
    obstacle = state / "synthetic-cache"
    obstacle.mkdir()
    (obstacle / "retained").write_text("retry me")
    real_rmtree = cleanup.shutil.rmtree
    failures = []

    blocked = {"on": True}
    def fail_cleanup(path, *args, **kwargs):
        if Path(path) == obstacle and blocked["on"]:
            failures.append(str(path))
            raise PermissionError("synthetic transient filesystem error")
        return real_rmtree(path, *args, **kwargs)

    class Active:
        def cancel(self):
            pass

    monkeypatch.setattr(cleanup.shutil, "rmtree", fail_cleanup)
    try:
        with extension_work_scope(loaded_provider("openai_realtime:openai"), Active(), retain_imports=True):
            result = cleanup.delete_local_skill(drive, skill)
            assert result["state_delete_pending"]
        assert failures and state.exists()
        assert skill.name not in _retiring_imports and realtime_load_refusal(skill.name) == ""
        assert (state / cleanup.UNINSTALL_TOMBSTONE_FILENAME).exists()
        blocked["on"] = False
        report = cleanup.sweep_uninstalled_skill_state(drive)
        assert report["swept"] == [skill.name] and not state.exists(), report
    finally:
        _receipt("cleanup-retry", {"failures": failures, "state_exists": state.exists(),
                                 "retiring": skill.name in _retiring_imports})
