"""A second launch of another copy names the running one from that instance's own report (``launcher_background``).

The launcher shows this one native line before it activates the running window (``tests/test_launcher_background.py``).
"""
from __future__ import annotations

import contextlib
import io
import json

from ouroboros import launcher_background as lb
from ouroboros import platform_layer


def test_the_second_copy_line_names_the_running_instance_by_its_own_report(tmp_path, monkeypatch):
    """Its process names its copy and its server its version; the same copy, an unreadable lock or an AppImage
    (whose per-launch mount names no copy) say nothing."""
    lock, port = tmp_path / "ouroboros.pid", tmp_path / "port"
    lock.write_text("4242", encoding="utf-8")
    port.write_text("8765", encoding="utf-8")
    old_app, new_app = tmp_path / "Desktop" / "Ouroboros.app", tmp_path / "Applications" / "Ouroboros.app"
    command = f"{old_app}/Contents/MacOS/ouroboros-shell /repo/launcher.py --seed-bundle {old_app}/Contents/Frameworks"
    monkeypatch.setattr(platform_layer, "process_command", lambda pid: command if pid == 4242 else "")
    monkeypatch.delenv("APPIMAGE", raising=False)
    health = json.dumps({"status": "ok", "app_version": "7.9.0"}).encode()
    monkeypatch.setattr(lb.urllib.request, "urlopen", lambda url, timeout: contextlib.nullcontext(io.BytesIO(health)))
    line = lb.other_copy_line(lock, port, new_app / "Contents" / "Frameworks")
    assert line == f"Ouroboros 7.9.0 is already running from {old_app}. Quit it, then open this copy."
    assert lb.other_copy_line(lock, port, old_app / "Contents" / "Frameworks") == "", "the same copy: unchanged"
    lock.write_text("not a pid", encoding="utf-8")
    assert lb.other_copy_line(lock, port, new_app / "Contents" / "Frameworks") == ""
    lock.write_text("4242", encoding="utf-8")
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "Ouroboros.AppImage"))
    assert lb.other_copy_line(lock, port, new_app / "Contents" / "Frameworks") == ""
