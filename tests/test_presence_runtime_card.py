"""Presence cards omit implicit execution settings and preserve explicit choices."""
import pytest

from tests.test_extensions_api import (
    _clean_extensions as _clean_extensions,
    _make_client,
    _stop_patches,
    _write_ext,
)


@pytest.mark.parametrize("execution", ["event", "call"])
@pytest.mark.parametrize("override", [None, "event", "call"])
def test_runtime_card_keeps_call_defaults_and_owner_execution_choices(tmp_path, monkeypatch, execution, override):
    from ouroboros.presence_capabilities import load_presence_state
    from ouroboros.presence_profile import parse_presence_profile
    from ouroboros.presence_runtime import resolve_presence_runtime
    from ouroboros.skill_loader import SkillReviewState, load_skill, save_review_state

    skills_root = tmp_path / "skills"
    skill_dir = _write_ext(
        skills_root, "presence_ext", permissions=[], plugin="def register(api):\n    pass\n",
        presence=("presence:\n  instructions: Participate when useful.\n"
                  f"  runtime_defaults:\n    execution: {execution}\n"),
    )
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        loaded = load_skill(skill_dir, drive_root)
        assert loaded is not None
        save_review_state(drive_root, loaded.name, SkillReviewState(status="pass", content_hash=loaded.content_hash))
        def card():
            rows = client.get("/api/extensions").json()["skills"]
            return next(row["presence_runtime"] for row in rows if row["name"] == loaded.name)
        initial = card()
        response = client.post("/api/owner/skills/presence_ext/presence-runtime", json={
            "expected_state_fingerprint": initial["state_fingerprint"],
            "runtime_overrides": {"model_slot": None, "inline_max_rounds": None, "execution": override},
        })
        assert response.status_code == 200, response.text
        runtime = response.json()["presence_runtime"]
        expected_defaults = {"model_slot": "main", "inline_max_rounds": 10}
        if execution == "call":
            expected_defaults["execution"] = "call"
        expected_overrides = {"model_slot": None, "inline_max_rounds": None}
        if override is not None:
            expected_overrides["execution"] = override
        assert runtime["defaults"] == initial["defaults"] == expected_defaults
        assert runtime["overrides"] == expected_overrides
        assert card() == runtime
        state = load_presence_state(drive_root, loaded.name)
        profile = parse_presence_profile(loaded.manifest, skill_dir)
        resolved = resolve_presence_runtime(profile.runtime_defaults, state.runtime_overrides, global_max_rounds=10)
        assert resolved.execution == (execution if override is None else override)
    finally:
        _stop_patches(patches)
