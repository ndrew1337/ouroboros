"""Automatic call-skill replacement waits; an explicit disable still cancels work."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ouroboros import extension_registry_state as registry


@pytest.fixture(params=["realtime_providers", "conversation_features"])
def publication(monkeypatch, request, tmp_path):
    name = "synthetic-call-skill"
    bundle = registry._ExtensionRegistrations(
        content_hash="old", skill_dir=str(tmp_path), generation_digest="generation-one")
    getattr(bundle, request.param).append(f"{name}:call")
    monkeypatch.setitem(registry._extensions, name, bundle)
    return name, bundle, {"skill": name, "extension_generation": bundle.generation_digest}


class Work:
    cancelled = False

    def cancel(self):
        self.cancelled = True


def test_replacement_refusal_lasts_only_while_call_work_is_live(publication):
    name, bundle, spec = publication
    background = Work()
    bundle.supervised_futures.append(background)
    assert registry.realtime_load_refusal(name) == ""
    work = Work()
    with registry.extension_work_scope(spec, work):
        assert registry.realtime_load_refusal(name) == "live_call_active"
        assert work in bundle.supervised_futures and not work.cancelled
    assert registry.realtime_load_refusal(name) == ""
    assert bundle.supervised_futures == [background]


def test_reconcile_retries_replacement_and_disable_ends_call(publication, monkeypatch, tmp_path):
    from ouroboros import extension_loader as loader

    name, bundle, spec = publication
    subject = SimpleNamespace(name=name)
    current = {"skill": name, "enabled": True, "desired_live": True,
               "loaded_present": True, "live_loaded": False, "reason": "ready"}
    monkeypatch.setattr(loader, "runtime_state_for_skill_name", lambda *a, **kw: dict(current))
    monkeypatch.setattr(loader, "_finalize_extension_reconcile", lambda *a, **kw: None)
    loaded = []
    monkeypatch.setattr(loader, "load_extension", lambda *a, **kw: loaded.append(a[0]) or None)
    reconcile = lambda: loader.reconcile_extension(name, tmp_path, lambda: {}, skills=[subject], repo_path="")
    work = Work()
    with registry.extension_work_scope(spec, work):
        refused = reconcile()
        assert refused["reason"] == "live_call_active"
        assert refused["action"] == "extension_reload_pending"
        assert refused["loaded_present"] and not loaded and not work.cancelled
        # An edited payload can lose current review authority; automatic reconcile
        # still leaves the running publication alone until the call ends.
        current.update(desired_live=False, reason="review_stale")
        assert reconcile()["reason"] == "live_call_active"
        current.update(desired_live=True, reason="ready")
    assert reconcile()["action"] == "extension_loaded"
    assert loaded == [subject]

    monkeypatch.setitem(registry._extensions, name, bundle)
    with registry.extension_work_scope(spec, work):
        current.update(enabled=False, desired_live=False, reason="disabled")
        assert reconcile()["action"] == "extension_unloaded"
        assert work.cancelled and name not in registry._extensions
    assert registry.realtime_load_refusal(name) == ""


def test_new_work_cannot_attach_to_a_replaced_publication(publication):
    from ouroboros.contracts.plugin_api import ExtensionRegistrationError

    _name, bundle, spec = publication
    bundle.generation_digest = "generation-two"
    with pytest.raises(ExtensionRegistrationError, match="generation changed"):
        with registry.extension_work_scope(spec, Work()):
            pytest.fail("stale call work was admitted")
    with registry.extension_work_scope({**spec, "extension_generation": "generation-two"}, Work()):
        assert registry.realtime_load_refusal(spec["skill"]) == "live_call_active"
