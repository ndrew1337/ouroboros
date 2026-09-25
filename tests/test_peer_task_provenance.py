"""peer_task provenance: a contribution from inside the tree without authority.

A sibling or a child writing to its parent is rendered under a prefix that names
the RELATION, never "ancestor" or "owner"; it wakes the mind but enters no owner
corpus and records no directive (serial addressed turns, first delivery).
"""

from __future__ import annotations

import json
import queue
from types import SimpleNamespace

import pytest

from ouroboros.loop_messages import _initialize_owner_directives, owner_authority_kinds
from ouroboros.loop_round_limits import _drain_incoming_messages
from ouroboros.owner_mailbox import (
    CONTEXT_ONLY_TASK_PROVENANCES,
    PEER_TASK_RELATIONS,
    PROVENANCE_PEER_TASK,
    TASK_MESSAGE_PROVENANCES,
    deliver_task_message,
    drain_owner_entries,
    write_task_message,
)


def test_peer_task_is_a_closed_context_only_provenance():
    assert PROVENANCE_PEER_TASK == "peer_task"
    assert PROVENANCE_PEER_TASK in TASK_MESSAGE_PROVENANCES
    assert PROVENANCE_PEER_TASK in CONTEXT_ONLY_TASK_PROVENANCES
    assert PEER_TASK_RELATIONS == frozenset({"sibling", "parent"})


@pytest.mark.parametrize("relation, label", [
    ("sibling", "[Message from peer task sib-1 (sibling)]"),
    ("parent", "[Message from peer task kid-1 (your child)]"),
    ("", "[Message from peer task kid-1]"),
])
def test_render_prefix_names_the_relation_never_ancestor_or_owner(relation, label):
    source = "sib-1" if relation == "sibling" else "kid-1"
    entry = {"provenance": PROVENANCE_PEER_TASK, "source_task_id": source,
             "text": "interim position: prefer the smaller change", "msg_id": "m1"}
    if relation:
        entry["relation"] = relation
    rendered, events = [], queue.Queue()

    deliver_task_message(entry, "recipient", events, rendered.append)

    [text] = rendered
    prefix = text.splitlines()[0]
    assert prefix == label
    assert "ancestor" not in prefix.lower() and "owner" not in prefix.lower()
    assert "human" not in prefix.lower()
    assert text.endswith("interim position: prefer the smaller change")
    event = events.get_nowait()
    assert event["type"] == "task_message_injected"
    assert event["provenance"] == PROVENANCE_PEER_TASK
    assert event["source_task_id"] == source
    assert event.get("relation", "") == relation


def test_relation_is_stored_at_write_and_projected_by_the_drain(tmp_path):
    assert write_task_message(tmp_path, "from a sibling", "recipient", source_task_id="sib-1",
                              provenance=PROVENANCE_PEER_TASK, relation="sibling", msg_id="p1")
    assert write_task_message(tmp_path, "from a child", "recipient", source_task_id="kid-1",
                              provenance=PROVENANCE_PEER_TASK, relation="parent", msg_id="p2")
    assert write_task_message(tmp_path, "unrelated", "recipient", source_task_id="root-1", msg_id="a1")

    rows = {row["msg_id"]: row for row in drain_owner_entries(tmp_path, "recipient")}
    assert rows["p1"]["provenance"] == PROVENANCE_PEER_TASK and rows["p1"]["relation"] == "sibling"
    assert rows["p2"]["provenance"] == PROVENANCE_PEER_TASK and rows["p2"]["relation"] == "parent"
    assert "relation" not in rows["a1"]  # stored only when given
    # Written bytes: the relation is a field of the entry, never parsed from text.
    raw = [json.loads(line) for line in
           (tmp_path / "memory" / "owner_mailbox" / "recipient.jsonl").read_text().splitlines()]
    assert [row.get("relation") for row in raw] == ["sibling", "parent", None]


def test_a_drained_peer_contribution_records_no_owner_directive(tmp_path):
    """Mirror of the corpus test in test_loop_misc: a peer's words are delivered
    under their own prefix and grow NOTHING in ``_owner_directives`` — the count
    that supersedes a paid acceptance verdict — and carry no owner-authority kind."""
    ctx = SimpleNamespace()
    messages = [{"role": "user", "content": "Initial requirement verbatim"}]
    _initialize_owner_directives(ctx, messages)
    write_task_message(tmp_path, "Objection: the closure code lets the author close it alone.", "child",
                       source_task_id="sib-7", provenance=PROVENANCE_PEER_TASK, relation="sibling",
                       msg_id="peer-1")
    write_task_message(tmp_path, "Interim: I would drop the mailbox redesign.", "child",
                       source_task_id="kid-2", provenance=PROVENANCE_PEER_TASK, relation="parent",
                       msg_id="peer-2")
    pending = drain_owner_entries(tmp_path, "child", seen_ids=set())
    assert [row["msg_id"] for row in pending] == ["peer-1", "peer-2"]
    assert owner_authority_kinds(pending) == []

    before = len(ctx._owner_directives)
    _drain_incoming_messages(messages, queue.Queue(), tmp_path, "child", None, set(), owner_ctx=ctx)

    assert len(ctx._owner_directives) == before
    delivered = "\n".join(str(m["content"]) for m in messages)
    assert "[Message from peer task sib-7 (sibling)]" in delivered
    assert "[Message from peer task kid-2 (your child)]" in delivered
    assert "the closure code lets the author close it alone" in delivered
    assert "ancestor task sib-7" not in delivered and "ancestor task kid-2" not in delivered
