"""A skill's Python environment follows the app that runs it (``marketplace/isolated_deps.py``).

A skill venv names its base interpreter (``pyvenv.cfg`` ``home`` and, on POSIX, an absolute
interpreter link). After the app moves, remounts or is re-extracted that base is gone: the
reader says ``stale`` with the binding, the dependency owner re-points the same major.minor
without pip, and rebuilds through the installer only for another readable major.minor.
Real venvs on ``sys.executable`` in tmp, no network.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ouroboros.marketplace import isolated_deps
from ouroboros.marketplace.install_specs import install_specs_hash
from ouroboros.skill_dependencies import ensure_skill_dependencies, skill_deps_not_ready
from ouroboros.skill_loader import skill_state_dir
from ouroboros.tools.skill_exec import _skill_deps_exec_block
from ouroboros.utils import atomic_write_json

SPECS = [{"kind": "pip", "package": "sentinel-pkg"}]
WINDOWS = os.name == "nt"
SITE = ("Lib/site-packages" if WINDOWS else "lib/python{}.{}/site-packages".format(*sys.version_info[:2]))


class _Admitted:
    def gate_for(self, _content_hash):
        return {"executable_review": True}


@pytest.fixture
def skill(tmp_path):
    """A reviewed skill whose installed pip package lives in a real venv made by this interpreter."""
    skill_dir, drive = tmp_path / "skill", tmp_path / "data"
    venv = skill_dir / ".ouroboros_env" / "python"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True,
                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    (venv / SITE / "sentinel_pkg.py").write_text("VALUE = 29\n", encoding="utf-8")
    bin_dir = venv / ("Scripts" if WINDOWS else "bin")
    if not WINDOWS:  # what pip writes for a console script: the venv's own python in the shebang
        (bin_dir / "sentinel").write_text(f"#!{bin_dir / 'python'}\nimport sentinel_pkg\nprint(sentinel_pkg.VALUE)\n",
                                          encoding="utf-8")
        (bin_dir / "sentinel").chmod(0o755)
    (skill_dir / ".ouroboroshub.json").write_text('{"install_specs": {"auto": [{"kind": "pip", "package": "sentinel-pkg"}]}}',
                                                  encoding="utf-8")
    record = {"status": "installed", "specs_hash": install_specs_hash(SPECS),
              "installed": [{"kind": "pip", "package": "sentinel-pkg", "installed": True, "outputs": []}]}
    atomic_write_json(skill_dir / ".ouroboros_env" / "fingerprint.json", record)
    atomic_write_json(skill_state_dir(drive, "demo") / "deps.json", record)
    loaded = SimpleNamespace(name="demo", skill_dir=skill_dir, source="local", content_hash="h",
                             review=_Admitted(), manifest=SimpleNamespace(raw_extra={}))
    return SimpleNamespace(drive=drive, loaded=loaded, venv=venv, bin=bin_dir, gone=tmp_path / "Old.app" / "bin")


def _move_host(skill, *, copied=False):
    """The app that made the venv is gone: its ``home`` and (POSIX) its interpreter entry name a dead path."""
    cfg = skill.venv / "pyvenv.cfg"
    cfg.write_text(re.sub(r"(?m)^home = .*$", f"home = {skill.gone}", cfg.read_text(encoding="utf-8")), encoding="utf-8")
    if WINDOWS:
        return
    for name in ("python", "python3", "python{}.{}".format(*sys.version_info[:2])):
        (skill.bin / name).unlink(missing_ok=True)
    if copied:  # a rollback snapshot's shape: a copied interpreter file, siblings linked to it
        (skill.bin / "python3").write_bytes(b"\x00copied interpreter of the old app\x00")
    else:
        (skill.bin / "python3").symlink_to(skill.gone / "python3")
    (skill.bin / "python").symlink_to("python3")


def _imports(skill) -> str:
    python = skill.bin / ("python.exe" if WINDOWS else "python")
    done = subprocess.run([str(python), "-c", "import sentinel_pkg; print(sentinel_pkg.VALUE)"],
                          capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    return done.stdout.strip()


def _state(skill):
    return isolated_deps.read_deps_state(skill.drive, "demo", skill.loaded.skill_dir)


@pytest.mark.parametrize("copied", [False, True], ids=["dangling-link", "copied-binary"])
def test_a_moved_env_reads_stale_and_its_owner_repoints_it_without_pip(skill, monkeypatch, copied):
    assert isolated_deps.python_env_binding(skill.loaded.skill_dir) == "current"
    assert _state(skill)["status"] == "installed" and _imports(skill) == "29"
    _move_host(skill, copied=copied)
    assert isolated_deps.python_env_binding(skill.loaded.skill_dir) == "moved"
    stale = _state(skill)
    assert (stale["status"], stale["python_env"]) == ("stale", "moved"), "the old reader said installed"
    assert skill_deps_not_ready(skill.drive, skill.loaded) == ("stale", "status"), "the startup pass picks it up"
    assert "SKILL_EXEC_BLOCKED" in _skill_deps_exec_block(skill.drive, skill.loaded), "never PATH python without its packages"
    assert isolated_deps.read_deps_state(skill.drive, "demo")["status"] == "installed", "the reader wrote nothing"

    def no_reinstall(*_args, **_kwargs):
        raise AssertionError("a moved env keeps its packages: no pip, no network")

    monkeypatch.setattr(isolated_deps, "install_isolated_dependencies", no_reinstall)
    assert ensure_skill_dependencies(skill.drive, skill.loaded) == ("installed", "")
    assert isolated_deps.python_env_binding(skill.loaded.skill_dir) == "current"
    assert _state(skill)["status"] == "installed" and _imports(skill) == "29"
    assert _skill_deps_exec_block(skill.drive, skill.loaded) == ""
    if not WINDOWS:
        assert (skill.bin / "python3").is_symlink(), "the old entry went first: venv --upgrade keeps an existing one"
        console = subprocess.run([str(skill.bin / "sentinel")], capture_output=True, text=True,
                                 env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        assert console.stdout.strip() == "29", "a console script names the venv's own python, which works again"

    def no_subprocess(*_args, **_kwargs):
        raise AssertionError("a current env needs no repair")

    monkeypatch.setattr(isolated_deps, "_run", no_subprocess)
    assert ensure_skill_dependencies(skill.drive, skill.loaded) == ("installed", "")


def test_another_minor_is_rebuilt_by_the_installer_and_an_unreadable_cfg_is_kept(skill, monkeypatch):
    real_run, pip = isolated_deps._run, []

    def run(cmd, **kwargs):
        if list(cmd[1:4]) == ["-m", "pip", "install"]:
            pip.append(cmd)
            return {"cmd": cmd[:2], "returncode": 0}
        if list(cmd[1:3]) == ["-m", "venv"] and "--upgrade" not in cmd:  # a real venv; the pip it seeds is not used here
            cmd = [*cmd[:3], "--without-pip", *cmd[3:]]
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(isolated_deps, "_run", run)
    cfg = skill.venv / "pyvenv.cfg"
    text = cfg.read_text(encoding="utf-8")
    cfg.unlink()
    assert isolated_deps.python_env_binding(skill.loaded.skill_dir) == "unreadable"
    assert _state(skill)["python_env"] == "unreadable"
    status, detail = ensure_skill_dependencies(skill.drive, skill.loaded)
    assert status == "failed" and "no readable pyvenv.cfg" in detail and not pip
    assert (skill.venv / SITE / "sentinel_pkg.py").exists(), "a failed read deletes nothing"

    cfg.write_text(re.sub(r"(?m)^version = .*$", "version = 3.1.0", text), encoding="utf-8")
    assert isolated_deps.python_env_binding(skill.loaded.skill_dir) == "other_python"
    assert ensure_skill_dependencies(skill.drive, skill.loaded) == ("installed", "")
    assert len(pip) == 1 and not (skill.venv / SITE / "sentinel_pkg.py").exists(), "rebuilt, then reinstalled once"
    assert isolated_deps.python_env_binding(skill.loaded.skill_dir) == "current"
    assert ensure_skill_dependencies(skill.drive, skill.loaded) == ("installed", "") and len(pip) == 1


def test_a_packageless_or_npm_only_skill_keeps_todays_runtime_choice(skill, tmp_path):
    _move_host(skill)
    record = {"status": "installed", "specs_hash": "npm", "installed": [{"kind": "npm", "package": "x", "installed": True}]}
    atomic_write_json(skill.loaded.skill_dir / ".ouroboros_env" / "fingerprint.json", record)
    atomic_write_json(skill_state_dir(skill.drive, "demo") / "deps.json", record)
    assert _state(skill)["status"] == "installed", "no Python package rides on that interpreter"
    (tmp_path / "plain").mkdir()
    plain = SimpleNamespace(name="plain", skill_dir=tmp_path / "plain", source="local", manifest=SimpleNamespace(raw_extra={}))
    assert _skill_deps_exec_block(skill.drive, plain) == "", "a packageless skill runs as before"


def test_extensions_import_only_this_interpreters_site_packages(tmp_path):
    from ouroboros.extension_isolated_deps import _isolated_python_site_dirs

    env = tmp_path / ".ouroboros_env" / "python"
    mine = env / SITE
    older = env / "lib" / "python3.1" / "site-packages"
    for path in (mine, older):
        path.mkdir(parents=True)
    assert _isolated_python_site_dirs(tmp_path) == [mine.resolve()], "an older Python's packages are not importable here"
