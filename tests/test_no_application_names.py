"""Nothing Ouroboros tracks carries a name macOS types as an application.

CoreTypes declares bundles with these suffixes as applications, so LaunchServices registers every
copy of such a path (each checkout, worktree and review copy) and Spotlight lists it among the apps
as a crossed-out ghost. A file that ships under such a name keeps a neutral source name instead
(``packaging/systemd/ouroboros.service.in`` installs as ``ouroboros.service``).
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
APPLICATION_SUFFIXES = (".app", ".service", ".placeholder", ".remoteplaceholder")


def application_shaped(paths) -> list[str]:
    """The paths with a component macOS would register as an application."""
    return [path for path in paths
            if any(part.lower().endswith(APPLICATION_SUFFIXES) for part in pathlib.PurePosixPath(path).parts)]


def test_the_rule_names_application_components_and_nothing_else():
    assert application_shaped(["packaging/systemd/ouroboros.service", "a/Ouroboros.app/Contents/x", "b/x.Placeholder",
                               "c/y.remoteplaceholder", "packaging/systemd/ouroboros.service.in", "web/app.js",
                               "tests/test_app.py", "docs/services/x.md"]) == [
        "packaging/systemd/ouroboros.service", "a/Ouroboros.app/Contents/x", "b/x.Placeholder", "c/y.remoteplaceholder"]


def test_no_tracked_path_is_application_shaped():
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True)
    if listed.returncode != 0:
        pytest.skip("not a Git checkout")
    paths = [path for path in listed.stdout.decode("utf-8", "surrogateescape").split("\0") if path]
    assert paths and application_shaped(paths) == []


def test_the_rule_claims_what_is_enforced_and_no_more():
    """Tracked names are checked here; a generated tree that may hold such a name (the bytecode mirror, a
    safe_test root) is born ``.noindex`` instead. The books never claim generated names are clean."""
    development = (REPO / "docs/development/10-platform-abstraction-rule.md").read_text(encoding="utf-8")
    invariants = (REPO / "docs/architecture/10-key-invariants.md").read_text(encoding="utf-8")
    assert "no tracked name is application-typed" in development and "trees that may hold one" in development
    assert "no tracked name is application-typed" in invariants
    assert "generated name" not in development + invariants
