"""Real owner-room WS -> admitted native actor -> full preview -> funded fake peer.

Synthetic content and dollars only. Run with safe_test, explicitly selected;
raw receipts and captures are exported before the isolated server is released.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from devtools.benchmarks.common.server_runner import _api, seed_owner_state
from tests.candidate_checkout import (
    candidate_checkout, assert_served_candidate, require_candidate_interpreter, verify_checkout,
)
from tests.system_e2e.harness import (
    KeylessIsolatedServer, ScriptedStubModel, keyless_settings, write_settings_file,
)
from tests.test_realtime_setup import documented_output_limit
from tests.test_realtime_turn import wait_for, transport_request

SOURCE = Path(__file__).resolve().parents[1]


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def post_receipt(server, path, body):
    request = Request(server.base_url + path, data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read())
    except HTTPError as response:
        return response.code, json.loads(response.read())


class Peer:
    def __init__(self, fixture, evidence):
        self.fixture, self.evidence = fixture, evidence
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.run, name="synthetic-realtime-peer")
        self.port = None

    def run(self):
        asyncio.run(self.serve())

    async def serve(self):
        from websockets.legacy.server import serve
        self.loop = asyncio.get_running_loop()
        self.stop = asyncio.Event()

        async def handler(ws):
            async for raw in ws:
                with (self.evidence / "peer-wire.jsonl").open("a") as stream:
                    stream.write(raw + "\n")
                event = json.loads(raw)
                mode = json.loads(self.fixture.read_text())["mode"]
                if mode in {"slow_stop", "wait"}:
                    await ws.wait_closed()
                    return
                limit = (event.get("session") or event.get("response") or {}).get("max_output_tokens", "inf")
                if event["type"] in {"session.update", "response.create"} and not documented_output_limit(limit):
                    await ws.send(json.dumps({"type": "error", "error": {
                        "type": "invalid_request_error", "param": "max_output_tokens"}}))
                    continue
                if event["type"] == "session.update":
                    reply = {"type": "session.updated", "session": event["session"]}
                elif event["type"] == "conversation.item.create":
                    reply = {"type": "conversation.item.done", "item": event["item"],
                             "previous_item_id": None if event["previous_item_id"] == "root"
                             else event["previous_item_id"]}
                else:
                    if mode == "probe_wait":
                        (self.evidence / "probe-sent").write_text(event["type"])
                        await ws.wait_closed()
                        return
                    await ws.send(json.dumps({"type": "response.created", "response": {"id": "synthetic-response"}}))
                    reply = {"type": "response.done", "response": {
                        "id": "synthetic-response", "status": "completed",
                        "output": [{"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "SYNTHETIC PROBE ONLY"}]}],
                        "usage": {"input_tokens": 1234, "output_tokens": 8}}}
                await ws.send(json.dumps(reply))

        async with serve(handler, "127.0.0.1", 0, max_size=None) as server:
            self.port = server.sockets[0].getsockname()[1]
            self.ready.set()
            await self.stop.wait()

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(10)
        return self

    def __exit__(self, *args):
        self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(10)
        assert not self.thread.is_alive()


class Server(KeylessIsolatedServer):
    def start(self, ready_timeout=90):
        from ouroboros.process_containment import ProcessContainer

        assert self.candidate is not None
        verify_checkout(self.clone, self.candidate)
        self._patch_settings_ports()
        env = self._env()
        env["PYTHONPATH"] = str(self.clone)
        require_candidate_interpreter(sys.executable, env, self.clone)
        self.output = (self.data_root.parent / "server-output.txt").open("a")
        self._candidate_container = ProcessContainer()
        self.candidate.hold()
        self.proc = None
        try:
            self.proc = self._candidate_container.spawn(
                [sys.executable, str(self.clone / "tests/realtime_server_host.py")],
                cwd=self.clone, env=env, stdout=self.output, stderr=self.output)
            self._wait_ready(ready_timeout)
            assert_served_candidate(self.base_url, self.clone, self.data_root,
                                    self.proc.pid, self.candidate)
        except BaseException:
            self.stop()
            raise
        return self

    def stop(self):
        try:
            super().stop()
        finally:
            if getattr(self, "output", None):
                self.output.close()


def seed_provider(root, repo):
    from ouroboros.skill_loader import (
        find_skill, save_enabled, save_review_state, save_skill_grants, SkillReviewState,
    )
    # These are disposable fixture admissions, not a review/enable of the candidate
    # in the owner's installation. The unchanged production loader still checks them.
    skill = find_skill(root, "openai_realtime", repo_path=str(repo / "skills"))
    save_enabled(root, skill.name, True)
    save_review_state(root, skill.name, SkillReviewState(
        status="pass", content_hash=skill.content_hash,
        raw_result="SYNTHETIC TEST FIXTURE, NOT A PRODUCT REVIEW"))
    save_skill_grants(root, skill.name, ["OPENAI_API_KEY"], content_hash=skill.content_hash,
                      requested_keys=["OPENAI_API_KEY"],
                      granted_permissions=["realtime_provider"],
                      requested_permissions=["realtime_provider"])


def receive(ws, kind, evidence, *, timeout=30):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        message = json.loads(ws.recv(timeout=max(.01, until - time.monotonic())))
        with (evidence / "owner-ws.jsonl").open("a") as stream:
            stream.write(json.dumps(message, ensure_ascii=False) + "\n")
        if message.get("type") == kind:
            return message
    raise AssertionError(f"missing {kind}")


@pytest.mark.serial
def test_real_room_preview_funding_and_stop(tmp_path):
    from websockets.sync.client import connect
    from ouroboros.capability_evidence import record_owner_ack
    from ouroboros.response_limits import record_response_ack
    from ouroboros.usage_accounting import read_usage_records
    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.task_results import load_task_result
    from ouroboros.cancel_intents import active_intent

    assert os.environ.get("OUROBOROS_TEST_TEMP_ROOT"), "run via scripts/safe_test.py"
    evidence = Path(os.environ["OUROBOROS_TEST_EVIDENCE_ROOT"]) / "room-server"
    evidence.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "host"
    data = root / "data"
    data.mkdir(parents=True)
    fixture = data / "realtime-fixture.json"
    server = None
    with candidate_checkout(SOURCE, root / "repo", origin_proof=True) as candidate, \
            Peer(fixture, evidence) as peer, \
            ScriptedStubModel(final_answer="SYNTHETIC ORDINARY TEXT ANSWER") as text_model:
        repo = candidate.path
        config = {"evidence": str(evidence), "peer": f"ws://127.0.0.1:{peer.port}", "mode": "success"}
        write_json(fixture, config)
        settings = keyless_settings(text_model,
            OUROBOROS_CONTEXT_MODE="nano", OUROBOROS_SKILLS_REPO_PATH=str(repo / "skills"),
            OUROBOROS_MAX_WORKERS=1)
        write_settings_file(data / "settings.json", settings)
        seed_owner_state(data)
        seed_provider(data, repo)
        from tests._memory_inventory_shared import world
        world(data)  # wholly synthetic prior Main/Project/peer history, through real stores
        (data / "memory").mkdir(exist_ok=True)
        (data / "memory/identity.md").write_text("SYNTHETIC_IDENTITY: fixture bird is a kingfisher.\n")
        route = dict(provider="openai_realtime", model="openai::gpt-realtime-2.1",
                     base_url="wss://api.openai.com/v1/realtime")
        record_owner_ack(data, **route, window_tokens=128000, owner="synthetic fixture")
        record_response_ack(data, **route, max_output_tokens=32000)
        try:
            server = Server(candidate, data, data / "settings.json").start()
            write_json(evidence / "served.json", {
                **server.attestation, "source_identity": candidate.identity,
                "checkout_identity": candidate.checkout_identity,
                "pid": server.proc.pid, "origin_proof": True})
            with connect(server.base_url.replace("http", "ws") + "/ws", max_size=None) as ws:
                frame = dict(type="chat", content="Synthetic owner room input: remember the copper kite.",
                             chat_id=1, client_message_id="realtime-room-preview",
                             origin_message_ref={"client_message_id": "forged-peer"},
                             execution_transport=transport_request(timeout_s=60))
                ws.send(json.dumps({**frame, "execution_transport": {
                    **frame["execution_transport"], "owner": {"role": "owner"}}}))
                assert receive(ws, "realtime_refused", evidence)["code"] == "transport"
                assert not _api(server.base_url, "GET", "/api/state")["active_chat_activities"]
                assert read_usage_records(data) == []
                ws.send(json.dumps(frame))
                preview = receive(ws, "realtime_prepared", evidence)
                capture_raw = read_actor_source_bytes(data, preview["task_id"], preview["source_ref"])
                (evidence / "full-selected-capture.json").write_bytes(capture_raw)
                capture = json.loads(capture_raw)
                selected = json.loads(capture["selected"]["messages_json"])
                assert selected == capture["prepared_messages"]
                assert capture["origin_message_ref"]["client_message_id"] == frame["client_message_id"]
                assert any("copper kite" in json.dumps(message) for message in selected)
                assert "SYNTHETIC_IDENTITY" in capture["selected"]["messages_json"]
                for note in ("capability_delta_prompt_block", "dispatch_executor_note", "startup_receipt"):
                    marker = "SYNTHETIC_AFTER_PLAN_" + note
                    assert marker in capture["selected"]["messages_json"]
                    assert marker not in json.dumps(capture["context_fit_plan"])
                assert "Main talk." in capture["context_fit_plan"]["core"]["memory_view_json"]
                assert capture["selected"]["mode"] == "nano"
                assert capture["context_fit_plan"]["model"] == route["model"]
                assert not (evidence / "connects.jsonl").exists()
                assert text_model.calls == []
                assert read_usage_records(data) == []
                active = _api(server.base_url, "GET", "/api/state")["active_chat_activities"]
                assert any(row["activity_id"] == preview["task_id"] for row in active)
                # An ordinary same-id retry rejoins this intent, without a new
                # actor, a longer deadline, or a changed execution transport.
                ws.send(json.dumps(frame))
                echo = receive(ws, "chat", evidence)
                assert echo["client_message_id"] == frame["client_message_id"] and echo["role"] == "user"
                assert len(_api(server.base_url, "GET", "/api/state")["active_chat_activities"]) == 1
                ws.send(json.dumps({**frame, "execution_transport": {
                    **frame["execution_transport"], "model": "different-model"}}))
                assert receive(ws, "realtime_refused", evidence)["code"] == "transport_identity"
                ws.send(json.dumps({k: v for k, v in frame.items() if k != "execution_transport"}))
                assert receive(ws, "realtime_refused", evidence)["code"] == "transport_identity"
                assert text_model.calls == []
                command = dict(type="realtime_execute", task_id=preview["task_id"], chat_id=1,
                               wire_sha256=preview["wire_sha256"], reservation_usd=.5)
                ws.send(json.dumps({**command, "chat_id": 99}))
                assert receive(ws, "realtime_refused", evidence)["code"] == "not_found"
                ws.send(json.dumps({**command, "wire_sha256": "0" * 64}))
                assert receive(ws, "realtime_refused", evidence)["code"] == "wire_changed"
                ws.send(json.dumps(command))
                receive(ws, "realtime_authorized", evidence)
                finished = receive(ws, "realtime_finished", evidence)
                assert finished["execution_status"] == "ok", finished
                rows = read_usage_records(data, final_only=True)
                assert len(rows) == 1 and rows[0]["cost_usd"] == .004, rows
                assert rows[0]["candidate_manifest_ref"]["model_send_seal_version"] == 1, rows
                assert rows[0]["candidate_raw_sha256"] == preview["wire_sha256"], rows
                write_json(evidence / "funded-ledger.json", rows)
                wait_for(lambda: not _api(server.base_url, "GET", "/api/state")["active_chat_activities"])
                config["mode"] = "slow_stop"
                write_json(fixture, config)
                ws.send(json.dumps({**frame, "client_message_id": "realtime-room-stop"}))
                stopped_preview = receive(ws, "realtime_prepared", evidence)
                ws.send(json.dumps({**command, "task_id": stopped_preview["task_id"],
                                    "wire_sha256": stopped_preview["wire_sha256"]}))
                receive(ws, "realtime_authorized", evidence)
                wait_for(lambda: (evidence / "connects.jsonl").read_text().count("\n") == 2)
                response = _api(server.base_url, "POST",
                                f"/api/tasks/{stopped_preview['task_id']}/cancel",
                                {"stop_policy": "immediate"})
                write_json(evidence / "stop-receipt.json", response)
                assert response["cancel_state"] == "pending", response
                wait_for(lambda: (evidence / "teardown-entered").exists())
                active = _api(server.base_url, "GET", "/api/state")["active_chat_activities"]
                assert any(row["activity_id"] == stopped_preview["task_id"] for row in active)
                write_json(evidence / "active-during-teardown.json", active)
                during = load_task_result(data, stopped_preview["task_id"])
                write_json(evidence / "result-during-teardown.json", during)
                assert during["status"] == "running", during
                assert active_intent(data, stopped_preview["task_id"])
                (evidence / "teardown-release").write_text("release fixture teardown\n")
                final = receive(ws, "realtime_finished", evidence)
                assert final["execution_status"] == "cancelled", final
                wait_for(lambda: not _api(server.base_url, "GET", "/api/state")["active_chat_activities"])
                rows = read_usage_records(data, final_only=True)
                write_json(evidence / "final-ledger.json", rows)
                stopped = next(row for row in rows if row["task_id"] == stopped_preview["task_id"])
                assert stopped["state"] == "unresolved" and stopped.get("cost_usd") is None, stopped
                # The same custody change also protects an ordinary TEXT actor
                # after its message admission closes, while cleanup still runs.
                config["mode"] = "text_teardown"
                write_json(fixture, config)
                ws.send(json.dumps(dict(type="chat", chat_id=1, client_message_id="ordinary-text",
                                        content="Return a short synthetic text answer.")))
                wait_for(lambda: (evidence / "text-teardown-entered").exists())
                text_id = (evidence / "text-teardown-entered").read_text()
                status, receipt = post_receipt(server, f"/api/tasks/{text_id}/cancel",
                                              {"stop_policy": "immediate"})
                write_json(evidence / "ordinary-text-stop.json", dict(status=status, receipt=receipt))
                assert status == 202 and receipt["cancel_state"] == "pending", receipt
                assert text_model.calls, "the normal text loop must have reached its synthetic HTTP peer"
                assert any(row["activity_id"] == text_id
                           for row in _api(server.base_url, "GET", "/api/state")["active_chat_activities"])
                (evidence / "text-teardown-release").write_text("release ordinary text cleanup\n")
                wait_for(lambda: not _api(server.base_url, "GET", "/api/state")["active_chat_activities"])
                write_json(evidence / "ordinary-text-model-calls.json", text_model.calls)

                config["mode"] = "probe_wait"
                write_json(fixture, config)
                paused_frame = {**frame, "client_message_id": "realtime-room-pause"}
                ws.send(json.dumps(paused_frame))
                paused = receive(ws, "realtime_prepared", evidence)
                ws.send(json.dumps({**command, "task_id": paused["task_id"],
                                    "wire_sha256": paused["wire_sha256"]}))
                receive(ws, "realtime_authorized", evidence)
                wait_for(lambda: (evidence / "connects.jsonl").read_text().count("\n") == 3)
                wait_for(lambda: (evidence / "probe-sent").exists())
                status, receipt = post_receipt(server, f"/api/tasks/{paused['task_id']}/pause",
                                              {"request_id": "synthetic-pause-1"})
                write_json(evidence / "pause-receipt.json", dict(status=status, receipt=receipt))
                assert status == 409 and receipt["reason_code"] == "realtime_pause_unsupported", receipt
                from ouroboros.owner_pause import read_fence
                assert read_fence(data, paused["task_id"]) == {}
                assert load_task_result(data, paused["task_id"])["status"] == "running"
                post_receipt(server, f"/api/tasks/{paused['task_id']}/cancel", {"stop_policy": "immediate"})
                ended = receive(ws, "realtime_finished", evidence)
                assert ended["execution_status"] != "ok"
                wait_for(lambda: not _api(server.base_url, "GET", "/api/state")["active_chat_activities"])
                paused_result = load_task_result(data, paused["task_id"])
                write_json(evidence / "paused-result.json", paused_result)
                assert paused_result["metadata"]["execution_transport"] == paused_frame["execution_transport"]
                status, receipt = post_receipt(server, f"/api/tasks/{paused['task_id']}/continue",
                                              {"action_nonce": "synthetic-continue-1"})
                write_json(evidence / "continue-refusal.json", dict(status=status, receipt=receipt))
                assert status == 409 and receipt["reason_code"] == "realtime_restart_required", receipt
                # A different, already SENT probe loses its process. Retrying
                # its owner message must neither buy another probe nor use text.
                (evidence / "probe-sent").unlink()
                restart_frame = {**frame, "client_message_id": "realtime-room-restart"}
                ws.send(json.dumps(restart_frame))
                restarting = receive(ws, "realtime_prepared", evidence)
                ws.send(json.dumps({**command, "task_id": restarting["task_id"],
                                    "wire_sha256": restarting["wire_sha256"]}))
                receive(ws, "realtime_authorized", evidence)
                wait_for(lambda: (evidence / "probe-sent").exists())
            # Only this disposable server is restarted. Replaying the old owner
            # message or funding id must not recreate the lost runtime handle.
            before = read_usage_records(data)
            write_json(evidence / "before-restart-ledger.json", before)
            text_calls = len(text_model.calls)
            server.stop()
            server = Server(candidate, data, data / "settings.json").start()
            write_json(evidence / "restarted-server.json", server.attestation)
            with connect(server.base_url.replace("http", "ws") + "/ws", max_size=None) as ws:
                ws.send(json.dumps(restart_frame))
                assert receive(ws, "realtime_refused", evidence)["code"] == "realtime_restart_required"
                ws.send(json.dumps({**command, "task_id": restarting["task_id"],
                                    "wire_sha256": restarting["wire_sha256"]}))
                assert receive(ws, "realtime_refused", evidence)["code"] == "not_found"
                assert not _api(server.base_url, "GET", "/api/state")["active_chat_activities"]
                assert len(text_model.calls) == text_calls
                after = read_usage_records(data)
                assert {r["attempt_id"] for r in after} == {r["attempt_id"] for r in before}
                write_json(evidence / "after-restart-ledger.json", after)
                assert (evidence / "connects.jsonl").read_text().count("\n") == 4
        finally:
            (evidence / "teardown-release").write_text("fixture cleanup\n")
            (evidence / "text-teardown-release").write_text("fixture cleanup\n")
            if server is not None:
                server.stop()
            for name in ("server-output.txt",):
                if (root / name).exists():
                    shutil.copy2(root / name, evidence / name)
            if (data / "task_results").exists():
                shutil.copytree(data / "task_results", evidence / "task_results", dirs_exist_ok=True)
            if (data / "artifacts").exists():
                shutil.copytree(data / "artifacts", evidence / "artifacts", dirs_exist_ok=True)
