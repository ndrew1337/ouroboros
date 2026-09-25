"""Cumulative replacements belong to the correction's own delivered sources."""

from copy import deepcopy
import json

import pytest

from ouroboros import consolidator as c, knowledge as k, room_consolidation as rc
from ouroboros.tools.registry import ToolContext
from tests import test_consolidator_context_fit as fit_helpers

fit = fit_helpers.fit
TOPIC = "shared-understanding"


def _answer(content):
    return "I recorded the episode.\nKNOWLEDGE_ENTRIES_JSON: " + json.dumps([
        {"topic": TOPIC, "scope": "global", "content": content}])


class CorrectionLLM:
    """Scripted responses; all read, delivery, binding and publication are real."""

    def __init__(self, draft, corrected, *, correction_read="complete", draft_read=True,
                 before_correction=None, after_correction_read=None, expected_note=None):
        self.draft, self.corrected = draft, corrected
        self.correction_read, self.draft_read = correction_read, draft_read
        self.before_correction = before_correction
        self.after_correction_read = after_correction_read
        self.expected_note = expected_note
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        messages = kwargs["messages"]
        correction = messages[0]["content"].startswith("Compare this draft memory")
        if messages[-1]["role"] != "tool":
            if correction and self.before_correction:
                self.before_correction()
            mode = self.correction_read if correction else ("complete" if self.draft_read else "none")
            if mode != "none":
                args = {"topic": TOPIC, "scope": "global"}
                if mode == "partial":
                    args.update(start_char=0, end_char=12)
                return {"tool_calls": [{"id": "read-note", "type": "function", "function": {
                    "name": "knowledge_read", "arguments": json.dumps(args)}}]}, {"cost": 0.01}
        if correction and self.correction_read == "complete":
            # The scripted answer is permitted only after actual source delivery.
            assert self.expected_note in messages[-1]["content"]
            if self.after_correction_read:
                self.after_correction_read()
        return {"content": _answer(self.corrected if correction else self.draft)}, {"cost": 0.01}


def _setup(tmp_path, initial):
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_id="room-memory")
    address = k.resolve_knowledge_address(tmp_path, TOPIC, "global")
    original = k.write_knowledge_note(address, initial).current if initial else None
    return ctx, address, original


def _consolidate(ctx, llm, episode):
    room = rc.RoomSource("1", "Main", [{"text": episode}], episode)
    block, usage = rc.summarize_block(
        c._light_call(llm, ctx, {}), [room], first_ts="2026-09-01T10:00:00Z",
        last_ts="2026-09-01T10:01:00Z", knowledge_instruction=c.KNOWLEDGE_MAINTENANCE_PROMPT)
    assert block and block["rooms"][0]["content"] == "I recorded the episode."
    return usage["_knowledge_entries"]


CASES = [
    ("Alex maintains Alpha and prefers email.", "Alex sent a meeting invitation.",
     "Alex maintains Alpha and prefers email. Alex sent a meeting invitation; attendance is unknown."),
    ("Delivery D1 of report R1 was confirmed at 10:05. Reply is unknown.",
     "At 10:00 I was asked to send report R1.",
     "Delivery D1 of report R1 was confirmed at 10:05 after the 10:00 request. Reply is unknown."),
    ("Alpha owns ingestion; Beta owns storage; Gamma owns search.", "Alpha now owns validation too.",
     "Alpha owns ingestion and validation; Beta owns storage; Gamma owns search."),
]


@pytest.mark.parametrize("initial,episode,updated", CASES)
@pytest.mark.parametrize("correction_read", ["none", "partial", "complete"])
def test_correction_cannot_borrow_draft_read_and_can_preserve_cumulative_knowledge(
        tmp_path, fit, initial, episode, updated, correction_read):
    ctx, address, original = _setup(tmp_path, initial)
    # Without the full note the failure-shaped response reduces it to the episode.
    content = updated if correction_read == "complete" else episode
    llm = CorrectionLLM(updated, content, correction_read=correction_read,
                        expected_note=original.text)
    entries = _consolidate(ctx, llm, episode)
    outcome = c._write_knowledge_entries(address.shelf, entries, context=ctx)[0]
    current = k.read_knowledge_note(address)
    if correction_read == "complete":
        assert entries[0]["expected_revision"] == original.revision
        assert outcome["ok"] and current.text.endswith(updated)
    else:
        assert entries[0]["expected_revision"] is None
        assert outcome["reason"] == "revision_required" and not outcome["ok"]
        assert current.raw == original.raw
    # Both authoring stages receive the cumulative scope and range-read support.
    for call in (llm.calls[0], next(call for call in llm.calls
                                  if call["messages"][0]["content"].startswith("Compare this draft memory"))):
        assert c.KNOWLEDGE_MAINTENANCE_PROMPT in call["messages"][0]["content"]


def test_own_complete_read_can_revise_and_remove_obsolete_facts_without_draft_read(tmp_path, fit):
    ctx, address, original = _setup(tmp_path, "Alex owns Alpha. Old office: Building 7. Contact by email.")
    updated = "Alex now owns Beta. Contact by email."
    llm = CorrectionLLM(original.text, updated, draft_read=False, expected_note=original.text)
    entries = _consolidate(ctx, llm, "Alex moved from Alpha to Beta and asked to remove the obsolete office address.")
    assert entries[0]["expected_revision"] == original.revision
    assert c._write_knowledge_entries(address.shelf, entries, context=ctx)[0]["ok"]
    current = k.read_knowledge_note(address)
    assert current.text.endswith(updated)
    assert "Building 7" not in current.text and "owns Alpha" not in current.text
    history = (tmp_path / "memory" / "knowledge_history.jsonl").read_text(encoding="utf-8")
    assert "Building 7" in history and "now owns Beta" in history


@pytest.mark.parametrize("change_after_read", [False, True])
def test_correction_binds_its_current_revision_and_preserves_later_concurrent_changes(tmp_path, fit, change_after_read):
    ctx, address, original = _setup(tmp_path, "Draft-era understanding.")
    latest = "A newer established observation."

    def replace():
        assert k.write_knowledge_note(address, latest, expected_revision=original.revision).ok

    llm = CorrectionLLM(original.text, "A newer established observation with the new episode.",
                        before_correction=None if change_after_read else replace,
                        after_correction_read=replace if change_after_read else None,
                        expected_note=original.text if change_after_read else latest)
    entries = _consolidate(ctx, llm, "A new episode.")
    current = k.read_knowledge_note(address)
    assert entries[0]["expected_revision"] == (original.revision if change_after_read else current.revision)
    result = c._write_knowledge_entries(address.shelf, entries, context=ctx)[0]
    if change_after_read:
        assert not result["ok"] and result["reason"] == "revision_conflict"
        assert k.read_knowledge_note(address).raw == current.raw
    else:
        assert result["ok"]
        assert k.read_knowledge_note(address).text.endswith("with the new episode.")


def test_correction_can_create_a_new_nominated_note_without_reading(tmp_path, fit):
    ctx, address, _ = _setup(tmp_path, None)
    llm = CorrectionLLM("New observation.", "Corrected new observation.",
                        draft_read=False, correction_read="none")
    entries = _consolidate(ctx, llm, "A new observation.")
    assert entries[0]["expected_revision"] is None
    assert c._write_knowledge_entries(address.shelf, entries, context=ctx)[0]["ok"]
    assert k.read_knowledge_note(address).text.endswith("Corrected new observation.")


def test_unread_correction_failure_remains_visible_after_dialogue_publication(tmp_path, fit):
    ctx, address, original = _setup(tmp_path, "An established body of knowledge.")
    chat, blocks, meta = fit_helpers._paths(tmp_path)
    fit_helpers._write_chat(chat, text_size=0)
    llm = CorrectionLLM(original.text, "Only this episode.", correction_read="none")
    c.consolidate(chat, blocks, meta, llm, knowledge_context=ctx)
    stored = json.loads(blocks.read_text(encoding="utf-8"))[0]
    assert stored["knowledge_writes"][0]["reason"] == "revision_required"
    state = json.loads(meta.read_text(encoding="utf-8"))
    assert state["last_consolidated_offset"] == 100
    assert len(state["pending_knowledge_nominations"]) == 1
    assert state["pending_knowledge_nominations"][0]["reason"] == "revision_required"
    assert k.read_knowledge_note(address).raw == original.raw
