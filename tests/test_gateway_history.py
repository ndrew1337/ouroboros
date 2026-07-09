from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from ouroboros.gateway.history import make_chat_history_endpoint


def test_chat_history_preserves_subagent_lane_group_metadata(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    (logs / "progress.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-06-05T00:00:00Z",
                "content": "subagent queued",
                "task_id": "child1",
                "subagent_event": "scheduled",
                "model_lane": "review",
                "requested_model_lane": "review",
                "effective_model_lane": "review",
                "model": "review-a",
                "task_group_id": "group1",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"limit": "10"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    rec = next(item for item in payload if item.get("task_id") == "child1")
    assert rec["model_lane"] == "review"
    assert rec["requested_model_lane"] == "review"
    assert rec["effective_model_lane"] == "review"
    assert rec["model"] == "review-a"
    assert rec["task_group_id"] == "group1"


def test_chat_history_replays_delivered_document_row(tmp_path):
    """A persisted document chat row is replayed as a msg_type=document record so
    the frontend rebuilds the file bubble on reload from the durable URL."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-07-09T00:00:00Z",
                "direction": "out",
                "chat_id": 1,
                "user_id": 7,
                "text": "quarterly numbers",
                "type": "document",
                "filename": "report.pdf",
                "mime": "application/pdf",
                "download_url": "/api/files/download?path=Desktop/report.pdf",
                "caption": "quarterly numbers",
                "task_id": "t-doc",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text("", encoding="utf-8")

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"limit": "10"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    rec = next(item for item in payload if item.get("msg_type") == "document")
    assert rec["role"] == "assistant"
    assert rec["filename"] == "report.pdf"
    assert rec["mime"] == "application/pdf"
    assert rec["download_url"] == "/api/files/download?path=Desktop/report.pdf"
    assert rec["caption"] == "quarterly numbers"


def test_chat_history_preserves_subagent_accept_markers(tmp_path):
    """WS8 accept/count markers must survive chat-history replay (gateway contract)."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    (logs / "progress.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-06-08T00:00:00Z",
                "content": "subagent queued",
                "task_id": "child2",
                "subagent_event": "scheduled",
                "accepted": True,
                "active_subagent_count": 3,
                "max_active_subagents": 6,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"limit": "10"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    rec = next(item for item in payload if item.get("task_id") == "child2")
    assert rec["accepted"] is True
    assert rec["active_subagent_count"] == 3
    assert rec["max_active_subagents"] == 6


def test_chat_history_preserves_subagent_reconciliation_metadata(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    (logs / "progress.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-06-27T00:00:00Z",
                "content": "subagent queued behind active cap",
                "task_id": "child3",
                "subagent_event": "scheduled",
                "queued_behind_active_cap": True,
                "required_capabilities": ["shell", "vcs"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"limit": "10"})))
    payload = json.loads(response.body.decode("utf-8"))["messages"]

    rec = next(item for item in payload if item.get("task_id") == "child3")
    assert rec["queued_behind_active_cap"] is True
    assert rec["required_capabilities"] == ["shell", "vcs"]
