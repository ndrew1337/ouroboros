"""Ordinary close must not end as a SIGKILL or a false "supervisor died" alarm (#1142).

Two mechanisms, two halves:

* server half — ``server._SignalStopServer.handle_exit`` sets ``_supervisor_stop`` at the
  signal, and ``uvicorn.Config`` carries ``timeout_graceful_shutdown`` so the lifespan
  teardown (the terminal-custody write) starts inside the launcher's stop budget;
* launcher half — the POSIX graceful phase signals only the server PID, leaving the group
  SIGKILL fallback and the post-exit group sweep untouched.

The real-process test deliberately reproduces the OLD launcher behaviour (SIGTERM to the
whole group), because packaged launchers are immutable until the next release: the server
half must be self-sufficient against it.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_signal_handler_stops_the_supervisor_loop_at_the_signal():
    import uvicorn

    import server

    server._supervisor_stop.clear()
    try:
        instance = server._SignalStopServer(uvicorn.Config(lambda scope, receive, send: None))
        instance.handle_exit(signal.SIGTERM, None)
        assert server._supervisor_stop.is_set(), "the loop must learn about the teardown at the signal"
        assert instance.should_exit is True, "uvicorn's own graceful exit must still be requested"
        assert instance.force_exit is False
    finally:
        server._supervisor_stop.clear()
        server._exit_signalled.clear()


def test_embedded_host_service_server_never_takes_the_process_signal_handlers():
    """`Server.serve()` on the main thread installs the process handlers and forwards a
    captured signal to the previous owner only after IT has drained: an embedded server
    would hold SIGTERM behind its own unbounded drain. The Host Service must not."""
    import uvicorn

    import server

    embedded = server._embedded_uvicorn_server(uvicorn.Config(lambda scope, receive, send: None))
    before = signal.getsignal(signal.SIGTERM)
    with embedded.capture_signals():
        assert signal.getsignal(signal.SIGTERM) is before
    assert signal.getsignal(signal.SIGTERM) is before
    plain = uvicorn.Server(uvicorn.Config(lambda scope, receive, send: None))
    assert type(plain).capture_signals is not embedded.capture_signals  # the override is instance-local


def test_a_settings_save_landing_after_the_signal_cannot_revive_the_supervisor(monkeypatch):
    """The stop event is revivable state (crash revival, a fresh lifespan); the exit latch is not."""
    import server

    monkeypatch.setattr(server, "has_startup_ready_provider", lambda _settings: True)
    monkeypatch.setattr(server, "_supervisor_thread", None)
    server._supervisor_stop.set()
    server._exit_signalled.set()
    try:
        assert server._start_supervisor_if_needed({"OPENROUTER_API_KEY": "k"}) is False
        assert server._supervisor_stop.is_set(), "revival cleared the teardown's stop event"
        assert server._supervisor_thread is None
    finally:
        server._exit_signalled.clear()
        server._supervisor_stop.clear()


def test_a_generation_admitted_just_before_the_signal_never_runs(monkeypatch):
    """Admission and thread start are separate steps, so a save can pass the latch check a
    moment before SIGTERM; the generation must then end before its startup kill/spawn."""
    import server

    touched: list = []
    monkeypatch.setattr(server, "_apply_settings_to_env", lambda _s: touched.append("init"))
    server._exit_signalled.set()
    try:
        server._supervisor_generation({"OPENROUTER_API_KEY": "k"})
    finally:
        server._exit_signalled.clear()
    assert touched == []
    assert server._supervisor_thread is None


def test_main_server_bounds_the_graceful_drain_from_the_shared_constant():
    import inspect

    import server
    from ouroboros.runtime_limits import LAUNCHER_STOP_GRACE_SEC, SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_SEC

    source = inspect.getsource(server.main)
    assert "timeout_graceful_shutdown=SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_SEC" in source
    assert "_SignalStopServer(config)" in source
    # The drain plus the lifespan's bounded supervisor join must leave the launcher budget room
    # for the terminal-custody write itself.
    assert SERVER_GRACEFUL_SHUTDOWN_TIMEOUT_SEC + 2 < LAUNCHER_STOP_GRACE_SEC


def test_launcher_graceful_phase_signals_only_the_server_pid(monkeypatch):
    import launcher

    calls: list = []

    class Process:
        pid = 4242

        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout):
            calls.append(("wait", timeout))

    monkeypatch.setattr(launcher, "IS_WINDOWS", False)
    monkeypatch.setattr(launcher, "_agent_proc", Process())
    monkeypatch.setattr(launcher, "_agent_job", None)
    monkeypatch.setattr(launcher.os, "killpg", lambda *a, **k: calls.append("killpg"), raising=False)
    monkeypatch.setattr(launcher, "kill_process_tree", lambda proc, **kw: calls.append("kill_tree"))
    monkeypatch.setattr(launcher, "_cleanup_recorded_server_group_for_pid", lambda *a: None)
    launcher.stop_agent()
    assert calls == ["terminate", ("wait", launcher.LAUNCHER_STOP_GRACE_SEC)]
    assert not hasattr(launcher, "terminate_process_tree"), "the group SIGTERM helper left the launcher"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_json(url: str, key: str, timeout_sec: float) -> None:
    deadline = time.time() + timeout_sec
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 - local test server
                payload = json.loads(resp.read().decode("utf-8"))
                if payload.get("supervisor_error"):
                    raise RuntimeError(f"supervisor failed to initialize: {payload['supervisor_error']}")
                if payload.get(key) is True:
                    return
        except Exception as exc:  # pragma: no cover - diagnostic only
            last = str(exc)
        time.sleep(0.25)
    raise RuntimeError(f"{url} never reported {key}=true: {last}")


@pytest.mark.serial
@pytest.mark.skipif(sys.platform == "win32", reason="process groups and SIGTERM are POSIX")
def test_group_sigterm_reaches_terminal_custody_without_a_false_supervisor_alarm(tmp_path):
    """A real server in an isolated data root, SIGTERMed the way the OLD launcher does (whole
    group, so its Manager dies first) while a browser WebSocket is open: the lifespan teardown
    must still run inside the launcher budget and the owner chat must get no supervisor_failure."""
    from ouroboros.process_containment import ProcessContainer
    from ouroboros.runtime_limits import LAUNCHER_STOP_GRACE_SEC

    port = _free_port()
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    model = "openai-compatible::never-called"
    (data_dir / "settings.json").write_text(json.dumps({
        "OPENAI_COMPATIBLE_API_KEY": "shutdown-test-key",
        "OPENAI_COMPATIBLE_BASE_URL": f"http://127.0.0.1:{port + 2}/v1",
        "OUROBOROS_MODEL": model, "OUROBOROS_MODEL_LIGHT": model, "OUROBOROS_MODEL_FALLBACKS": model,
        "OUROBOROS_MAX_WORKERS": 1,
        "OUROBOROS_RUNTIME_MODE": "light",
    }), encoding="utf-8")
    # An owner chat is bound, so the false alarm WOULD be written if the crash counter fired.
    (data_dir / "state" / "state.json").write_text(json.dumps({"owner_chat_id": 1}), encoding="utf-8")
    env = {
        **os.environ,
        "HOME": str(home),
        "OUROBOROS_APP_ROOT": str(tmp_path),
        "OUROBOROS_DATA_DIR": str(data_dir),
        "OUROBOROS_SETTINGS_PATH": str(data_dir / "settings.json"),
        "OUROBOROS_REPO_DIR": REPO_ROOT,
        "OUROBOROS_SERVER_HOST": "127.0.0.1",
        "OUROBOROS_SERVER_PORT": str(port),
        "OUROBOROS_HOST_SERVICE_PORT": str(port + 1),
        "OUROBOROS_MANAGED_BY_LAUNCHER": "1",
    }
    url = f"http://127.0.0.1:{port}"
    container = ProcessContainer()
    proc = container.spawn(
        [sys.executable, "server.py"], cwd=REPO_ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    ws = None
    stuck = None
    try:
        _wait_json(f"{url}/api/state", "supervisor_ready", 90)
        time.sleep(1.0)  # let the loop run a few ticks against the live Manager
        from websockets.sync.client import connect

        ws = connect(f"ws://127.0.0.1:{port}/ws", open_timeout=10)  # the desktop window's socket
        # An in-flight request whose body never completes: uvicorn's graceful drain waits for
        # it, which is what parked the lifespan teardown behind the launcher budget.
        stuck = socket.create_connection(("127.0.0.1", port), timeout=5)
        stuck.sendall(
            b"POST /api/settings HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\nContent-Length: 100000\r\n\r\n{\"OUROBOROS_MODEL\": \""
        )
        time.sleep(0.5)
        started = time.monotonic()
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)  # exactly what the pre-#1142 launcher does
        try:
            code = proc.wait(timeout=LAUNCHER_STOP_GRACE_SEC)
        except subprocess.TimeoutExpired:
            pytest.fail("server did not exit within the launcher stop budget — the next launcher would SIGKILL it")
        elapsed = time.monotonic() - started
    finally:
        if stuck is not None:
            stuck.close()
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if proc.poll() is None:
            proc.kill()
        try:
            error = container.reap()
        finally:
            container.close()
        proc.wait(timeout=10)  # collect the direct child; reap() skips zombies by design
        assert not error, error

    # uvicorn re-raises the captured SIGTERM once the lifespan has completed, so a clean
    # signalled exit reads -SIGTERM (or 0); -SIGKILL is the launcher fallback this test forbids.
    assert code in (0, -signal.SIGTERM), f"graceful exit expected, got {code}"
    chat_rows = _read_rows(data_dir / "logs" / "chat.jsonl")
    alarms = [row for row in chat_rows if row.get("system_type") == "supervisor_failure"
              or "Supervisor loop died" in str(row.get("text") or "")]
    assert alarms == [], alarms
    shutdown_rows = [row for row in _read_rows(data_dir / "logs" / "supervisor.jsonl")
                     if row.get("type") == "server_shutdown"]
    assert shutdown_rows and shutdown_rows[-1].get("cause") == "external_signal", shutdown_rows
    assert elapsed < LAUNCHER_STOP_GRACE_SEC, elapsed
