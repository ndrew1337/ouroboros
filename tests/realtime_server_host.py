"""Keyless real-server fixture. Only the external provider connection is replaced.

The normal server, WS ingress, room routing, actor, context, control and money
owners run unmodified. Never use this launcher with personal data or credentials.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import runpy
import socket
import sys
import time


def main():
    root = Path(os.environ["OUROBOROS_DATA_DIR"])
    fixture = root / "realtime-fixture.json"
    assert fixture.is_file() and os.environ.get("OUROBOROS_TEST_TEMP_ROOT")
    repo = Path.cwd()
    sys.path.insert(0, str(repo))
    evidence = Path(json.loads(fixture.read_text())["evidence"])

    def record(name, row):
        with (evidence / name).open("a") as stream:
            stream.write(json.dumps(row) + "\n")

    # This is test instrumentation, not a transport option in the product.
    # All remote egress is denied even if an unexpected ordinary model path runs.
    original_connect = socket.socket.connect

    def guarded_connect(sock, address):
        if isinstance(address, tuple):
            host = address[0]
            try:
                allowed = ipaddress.ip_address(host).is_loopback
            except ValueError:
                allowed = host == "localhost"
            record("connections.jsonl", {"host": host, "port": address[1], "allowed": allowed})
            if not allowed:
                raise RuntimeError("synthetic server forbids non-loopback egress")
        return original_connect(sock, address)

    socket.socket.connect = guarded_connect
    from ouroboros import realtime_setup, usage_accounting
    from ouroboros.extension_plugin_api import PluginAPIImpl

    original_settings = PluginAPIImpl.get_settings

    def no_provider_credentials(api, *args, **kwargs):
        if api._skill == "openai_realtime":
            raise AssertionError("synthetic realtime consumer must never read credentials")
        return original_settings(api, *args, **kwargs)

    PluginAPIImpl.get_settings = no_provider_credentials
    # Make the three ordinary post-plan note sources observable. These markers
    # are synthetic fixture contents, never the owner's current personal input.
    from ouroboros import agent
    for name in ("capability_delta_prompt_block", "dispatch_executor_note"):
        original = getattr(agent, name)
        def marked(*args, _original=original, _name=name, **kwargs):
            return (_original(*args, **kwargs) or "") + "\nSYNTHETIC_AFTER_PLAN_" + _name
        setattr(agent, name, marked)
    original_receipt = agent.subagent_bootstrap.append_startup_receipt
    def startup_receipt(ctx, messages, wake):
        original_receipt(ctx, messages, wake)
        messages.append({"role": "user", "content": "SYNTHETIC_AFTER_PLAN_startup_receipt"})
    agent.subagent_bootstrap.append_startup_receipt = startup_receipt
    from ouroboros.tools import browser

    original_cleanup = browser.cleanup_browser

    def delayed_text_cleanup(ctx):
        original_cleanup(ctx)
        if json.loads(fixture.read_text())["mode"] == "text_teardown" and ctx.is_direct_chat:
            (evidence / "text-teardown-entered").write_text(ctx.task_id)
            while not (evidence / "text-teardown-release").exists():
                time.sleep(.05)

    browser.cleanup_browser = delayed_text_cleanup
    original_setup = realtime_setup.run_realtime_setup

    @asynccontextmanager
    async def connect(model):
        from websockets.legacy.client import connect as websocket_connect
        config = json.loads(fixture.read_text())
        record("connects.jsonl", {"model": model, "mode": config["mode"]})
        try:
            async with websocket_connect(config["peer"], max_size=None) as ws:
                yield ws
        finally:
            if config["mode"] == "slow_stop":
                (evidence / "teardown-entered").write_text("provider transport teardown entered\n")
                while not (evidence / "teardown-release").exists():
                    await asyncio.sleep(.05)
            record("teardowns.jsonl", {"model": model, "mode": config["mode"]})

    async def injected(*args, **kwargs):
        return await original_setup(*args, **{**kwargs, "connect": connect})

    realtime_setup.run_realtime_setup = injected
    # Explicit mock tariff; no public price fetch and no assertion about real bills.
    original_price = usage_accounting.estimate_cost_optional
    usage_accounting.estimate_cost_optional = lambda *args, **kwargs: (
        .004 if kwargs.get("provider") == "openai_realtime" else original_price(*args, **kwargs))
    record("boot.jsonl", {
        "kind": "synthetic-isolated-server", "pid": os.getpid(), "repo": str(repo), "data": str(root),
        "imports": {name: str(Path(sys.modules[name].__file__).resolve()) for name in
                    ("ouroboros.realtime_setup", "ouroboros.usage_accounting")},
        "source_sha256": {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for folder in ("ouroboros", "supervisor")
                          for path in (repo / folder).rglob("*.py")},
    })
    sys.argv = [str(repo / "server.py")]
    runpy.run_path(str(repo / "server.py"), run_name="__main__")


if __name__ == "__main__":
    main()
