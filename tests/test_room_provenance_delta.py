"""Owner-approved room provenance; all history and registry data are synthetic."""
from __future__ import annotations

import json

import pytest

from ouroboros import consolidator as c, projects_registry, room_consolidation as rc
from ouroboros.context import build_recent_sections
from ouroboros.dialogue_provenance import RoomLabelResolver, source_continuation_note
from ouroboros.memory import Memory
from tests.test_consolidator_context_fit import _LLM, fit as _fit

fit = _fit  # noqa: F811 - shared isolated Light route fixture re-export


def _write_chat(root, rows):
    path = root / "logs" / "chat.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    return path


def _registry(root, projects):
    path = root / "state" / "projects.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"projects": projects}), encoding="utf-8")
    return path


def _recent(memory, chat_id):
    sections = build_recent_sections(memory, None, thread_chat_id=chat_id)
    return next(s for s in sections if s.startswith("## Recent chat\n"))


def test_room_resolution_is_current_read_only_and_not_lineage(tmp_path, monkeypatch):
    project = projects_registry.create_project(tmp_path, "alpha", name="Original")
    projects_registry.update_project(tmp_path, "alpha", name="Renamed")
    path = tmp_path / "state" / "projects.json"
    before = path.read_bytes()
    read = projects_registry.list_reserved_projects
    calls = []
    monkeypatch.setattr(projects_registry, "list_reserved_projects", lambda root: (calls.append(root), read(root))[1])
    resolver = RoomLabelResolver(tmp_path)
    for _ in range(10):
        assert resolver.label({"chat_id": 1, "project_id": "alpha"}) == "Main"
        assert resolver.label({"chat_id": project["chat_id"], "project_id": "wrong-lineage"}) == (
            f"Project Renamed [chat_id={project['chat_id']}]"
        )
        assert resolver.label({"project_id": "alpha"}) == "Unresolved room [chat_id=missing]"
    assert calls == [tmp_path]
    assert path.read_bytes() == before


@pytest.mark.parametrize("lifecycle", ["active", "deleting", "tombstoned"])
def test_reserved_names_and_removed_or_ambiguous_rooms(tmp_path, lifecycle):
    row = {"id": "alpha", "chat_id": 1500, "name": "Alpha ] team", "lifecycle": lifecycle}
    path = _registry(tmp_path, [row])
    assert RoomLabelResolver(tmp_path).label({"chat_id": 1500}) == "Project Alpha ] team [chat_id=1500]"
    _registry(tmp_path, [{**row, "name": ""}])
    assert RoomLabelResolver(tmp_path).label({"chat_id": 1500}) == "Project name unavailable [chat_id=1500]"
    _registry(tmp_path, [row, {**row, "id": "other", "name": "Other"}])
    resolver = RoomLabelResolver(tmp_path)
    assert resolver.label({"chat_id": 1500}) == "Ambiguous room [chat_id=1500]"
    assert 1500 in resolver.project_chat_ids  # Label uncertainty must not widen focused visibility.
    _registry(tmp_path, [])
    assert RoomLabelResolver(tmp_path).label({"chat_id": 1500}) == "Unknown room [chat_id=1500]"
    path.unlink()
    assert RoomLabelResolver(tmp_path).label({"chat_id": 1500}) == "Unknown room [chat_id=1500]"
    assert not path.exists()


@pytest.mark.parametrize("entry,label", [
    ({}, "Unresolved room [chat_id=missing]"),
    ({"chat_id": None}, "Unresolved room [chat_id=missing]"),
    ({"chat_id": "oops"}, "Unresolved room [chat_id=oops]"),
    ({"chat_id": True}, "Unresolved room [chat_id=True]"),
    ({"chat_id": 1.2}, "Unresolved room [chat_id=1.2]"),
    ({"chat_id": "1"}, "Main"),
    ({"chat_id": 0}, "Hidden [chat_id=0]"),
    ({"chat_id": 987654}, "Unknown room [chat_id=987654]"),
])
def test_unknown_address_never_defaults_to_main(entry, label):
    assert RoomLabelResolver(projects=[]).label(entry) == label


def test_actual_main_context_opts_in_once_and_keeps_existing_visibility(tmp_path, monkeypatch):
    _registry(tmp_path, [{"id": "alpha", "chat_id": 1500, "name": "Alpha"}])
    rows = [
        {"chat_id": 1, "direction": "in", "text": "MAIN", "project_id": "alpha"},
        {"chat_id": 1500, "direction": "out", "text": "PROJECT"},
        {"chat_id": 987654, "direction": "system", "text": "UNKNOWN"},
        {"direction": "in", "text": "MISSING"},
        {"chat_id": 0, "direction": "system", "text": "HIDDEN"},
        {"chat_id": -10, "direction": "in", "text": "A2A EXCLUDED"},
    ]
    _write_chat(tmp_path, rows)
    memory = Memory(tmp_path)
    expected_rows, _ = memory.read_unconsolidated_chat({}, 1000)
    read = projects_registry.list_reserved_projects
    calls = []
    monkeypatch.setattr(projects_registry, "list_reserved_projects", lambda root: (calls.append(root), read(root))[1])
    recent = _recent(memory, 1)
    assert calls == [tmp_path]
    assert recent == "## Recent chat\n\n" + memory.summarize_chat(
        expected_rows, include_room_labels=True, room_resolver=RoomLabelResolver(projects=read(tmp_path)),
    )
    for marker in ("[room=Main]", "[room=Project Alpha [chat_id=1500]]",
                   "[room=Unknown room [chat_id=987654]]", "[room=Unresolved room [chat_id=missing]]"):
        assert marker in recent
    assert "A2A EXCLUDED" not in recent
    assert recent.index("MAIN") < recent.index("PROJECT") < recent.index("UNKNOWN") < recent.index("MISSING")


@pytest.mark.parametrize("ambiguous", [False, True])
def test_focused_project_and_explicit_history_remain_byte_identical(tmp_path, monkeypatch, ambiguous):
    monkeypatch.setattr("ouroboros.memory._chat_history_snapshot_id", lambda *_: "fixture")
    projects = [{"id": "alpha", "chat_id": 1500, "name": "Alpha"}]
    if ambiguous:
        projects.append({"id": "beta", "chat_id": 1500, "name": "Beta"})
    _registry(tmp_path, projects)
    base = {"ts": "2026-01-01T00:01:00Z", "direction": "in", "sender_label": "Alex"}
    _write_chat(tmp_path, [
        {**base, "chat_id": 1, "text": "main"},
        {**base, "chat_id": 1500, "text": "project\nsecond line", "transport": {"provider": "mail"}},
        {**base, "chat_id": 1501, "text": "sibling"},
        {**base, "chat_id": -10, "text": "a2a"},
    ])
    memory = Memory(tmp_path)
    assert _recent(memory, 1500).encode() == (
        "## Recent chat\n\n← 00:01 [Alex [provider=mail]] project\nsecond line"
    ).encode()
    expected_history = (
        "Showing 3 of 3 messages; 0 older remain. Continue with offset=3, snapshot=fixture."
        " Pagination used a live offset; repeating an offset without the returned snapshot"
        " is shiftable if history changes.\n\n"
        "← [2026-01-01T00:01] [Alex] main\n"
        "← [2026-01-01T00:01] [Alex [provider=mail]] project\nsecond line\n"
        "← [2026-01-01T00:01] [Alex] sibling"
    ).encode()
    for chat_id in (1, 1500):
        assert memory.chat_history(chat_id=chat_id).encode() == expected_history


@pytest.mark.parametrize("direction", ["in", "incoming", "out", "outgoing", "system"])
def test_block_format_retains_author_direction_transport_and_body(direction):
    row = {"ts": "2026-01-01T00:00:00Z", "chat_id": 1500, "direction": direction,
           "sender_label": "Alex", "text": "line one\r\n\r\nЖ🙂 line two\n",
           "transport": {"provider": "mail", "account_id": "acct", "conversation_id": "conv",
                         "thread_id": "thread", "delivery": {"state": "accepted"}}}
    resolver = RoomLabelResolver(projects=[{"id": "alpha", "chat_id": 1500, "name": "Alpha"}])
    old = c._format_entries_for_block([row])
    new = c._format_entries_for_block([row], include_room_labels=True, room_resolver=resolver)
    assert new.replace("[room=Project Alpha [chat_id=1500]] ", "", 1).encode() == old.encode()
    assert new.endswith(row["text"])
    assert "provider=mail; account=acct; conversation=conv; thread=thread; delivery=accepted" in new
    assert ("Ouroboros" if direction in {"out", "outgoing", "system"} else "Alex") in new


@pytest.mark.parametrize("rooms", [(1, 1, 1, 1), (1, 1500, 987654, None)])
def test_actual_consolidation_labels_every_source_and_retains_token_ceiling(tmp_path, fit, monkeypatch, rooms):
    _registry(tmp_path, [{"id": "alpha", "chat_id": 1500, "name": "Alpha"}])
    rows = [{"ts": f"2026-01-01T00:{i:02d}:00Z", "chat_id": room, "direction": "in", "text": str(i)}
            for i, room in enumerate(rooms)]
    chat = _write_chat(tmp_path, [*rows, {"chat_id": -10, "text": "A2A EXCLUDED"}])
    monkeypatch.setattr(c, "BLOCK_SIZE", 2)
    read = projects_registry.list_reserved_projects
    calls = []
    monkeypatch.setattr(projects_registry, "list_reserved_projects", lambda root: (calls.append(root), read(root))[1])
    llm = _LLM()
    c.consolidate(chat, tmp_path / "memory/blocks.json", tmp_path / "memory/meta.json", llm)
    assert calls == [tmp_path]
    # Each room is drafted and then source-checked; two logical chunks are
    # processed, with one or two rooms per chunk depending on the fixture.
    assert len(llm.calls) == (4 if len(set(rooms)) == 1 else 8)
    for call in llm.calls:
        assert call["max_tokens"] == 16384
        assert "A2A EXCLUDED" not in call["messages"][0]["content"]
        assert "[room=" in call["messages"][0]["content"]


def test_room_source_split_preserves_exact_bytes_and_boundaries():
    rows = [
        {"chat_id": 1500, "direction": "in", "text": "A body\n\n" * 80},
        {"chat_id": 1501, "direction": "out", "text": "B body\n\n" * 80},
    ]
    resolver = RoomLabelResolver(projects=[
        {"id": "alpha", "chat_id": 1500, "name": "Alpha"},
        {"id": "beta", "chat_id": 1501, "name": "Beta"},
    ])
    spans = []
    source = c._format_entries_for_block(rows, include_room_labels=True, room_resolver=resolver, source_spans=spans)
    left, right = rc.split_source_text(source, tuple(start for start, _, _ in spans))
    assert left + right == source
    assert right.startswith(spans[1][2])
    assert "[room=Fake]" not in source


def test_boundary_split_does_not_add_rooms_and_prompts_are_adaptive():
    spans = []
    source = c._format_entries_for_block([
        {"chat_id": 1, "text": "body\n\n" * 20},
        {"chat_id": 555, "text": "other\n\n" * 20},
    ], include_room_labels=True, source_spans=spans)
    left, right = rc.split_source_text(source, tuple(start for start, _, _ in spans))
    assert left + right == source and right.startswith(spans[1][2])
    assert source_continuation_note(spans, len(left), len(source)) == ""
    assert spans[0][2] in source_continuation_note(spans, 0, 2)
    prompt = rc.room_draft_prompt(source, room_label="test", block_range_text="range", message_count=2)
    assert "fixed total word range" not in prompt
    assert "First person as Ouroboros" in prompt and "source" in prompt


def test_era_prompt_preserves_rooms_with_original_token_ceiling(fit):
    llm = _LLM()
    era, _ = c._compress_blocks_to_era([
        {"range": "2026-01-01 00:00 - 00:01", "message_count": 1, "content": "Project A decision"},
        {"range": "2026-01-01 00:02 - 00:03", "message_count": 1, "content": "Project B approval"},
    ], llm, "")
    assert era and len(llm.calls) == 2  # legacy records share one unknown-provenance room
    call = llm.calls[0]
    prompt = call["messages"][0]["content"]
    assert "one room" in prompt and "other rooms are compressed separately" in prompt
    assert "open commitments" in prompt and "one first-person Ouroboros" in prompt
    assert call["max_tokens"] == 16384


def test_draft_nominations_are_released_only_with_their_corrected_part():
    """A draft whose correction failed never entered the block, so its
    nominations must not survive the split that replaces it (claim_2)."""
    spans = []
    rows = [{"ts": f"2026-01-01T00:0{i}:00Z", "direction": "in", "text": f"entry-{i} " + "Ж🙂x" * 40, "chat_id": 1}
            for i in range(2)]
    text = c._format_entries_for_block(rows, include_room_labels=True, source_spans=spans)
    spans = [(start, end, note) for start, end, note in spans]

    class _Knowledge:
        def bind_entries(self, entries):
            return list(entries or [])

    calls = []

    def call(prompt, label, *, fixed_prompt="", input_limit=None, call_type=""):
        calls.append((label, prompt))
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0}
        if label == "Room summary":
            nomination = "" if len(calls) > 1 else '\nKNOWLEDGE_ENTRIES_JSON: [{"topic":"leak","scope":"global","content":"from a discarded draft"}]'
            return f"draft-{len(calls)}{nomination}", usage, _Knowledge()
        if len(calls) == 2:  # the FIRST correction (whole source) overflows -> the part is split
            return "", {**usage, "_consolidation_errors": [{
                "kind": "context_overflow", "preflight_only": True, "message": "too big",
                "fixed_tokens": 1, "fixed_bytes": 1}]}, _Knowledge()
        return f"corrected-{len(calls)}", usage, _Knowledge()

    draft_prompt = lambda part, note: rc.room_draft_prompt(  # noqa: E731
        part, room_label="Main", block_range_text="r", message_count=2, identity_text="", continuation_note=note)
    correct_prompt = lambda draft, part, note: rc.correction_prompt(  # noqa: E731
        draft, part, room_label="Main", scope="block r", identity_text="", continuation_note=note)
    content, usage = rc.summarize_source(call, text, spans, draft_prompt, correct_prompt)

    assert content and "corrected-" in content
    labels = [label for label, _ in calls]
    assert labels == ["Room summary", "Room correction"] + ["Room summary", "Room correction"] * 2
    assert "_knowledge_entries" not in usage, usage.get("_knowledge_entries")


def test_correction_prefix_over_the_limit_still_splits_and_redrafts_the_halves():
    """A correction's fixed prompt carries the whole draft; when that alone
    exceeds the route limit the part is still split and each half re-drafted,
    instead of the chunk being withheld forever."""
    spans = []
    rows = [{"ts": f"2026-01-01T00:0{i}:00Z", "direction": "in", "text": f"entry-{i} " + "Ж🙂x" * 40, "chat_id": 1}
            for i in range(2)]
    text = c._format_entries_for_block(rows, include_room_labels=True, source_spans=spans)
    calls = []

    def call(prompt, label, *, fixed_prompt="", input_limit=None, call_type=""):
        calls.append(label)
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0}
        if len(calls) == 2:  # whole-source correction: its fixed prefix alone is over the limit
            return "", {**usage, "_consolidation_errors": [{
                "kind": "context_overflow", "preflight_only": True, "message": "too big",
                "fixed_tokens": 900, "input_limit": 500, "fixed_bytes": 1, "byte_limit": None}]}, None
        return f"{label}-{len(calls)}", usage, None

    draft_prompt = lambda part, note: rc.room_draft_prompt(  # noqa: E731
        part, room_label="Main", block_range_text="r", message_count=2, identity_text="", continuation_note=note)
    correct_prompt = lambda draft, part, note: rc.correction_prompt(  # noqa: E731
        draft, part, room_label="Main", scope="block r", identity_text="", continuation_note=note)
    content, _usage = rc.summarize_source(call, text, [(s, e, n) for s, e, n in spans], draft_prompt, correct_prompt)
    assert content and calls == ["Room summary", "Room correction"] + ["Room summary", "Room correction"] * 2


def test_room_labels_enter_prompts_as_one_quoted_json_string():
    label = 'Alpha ] team\n## Rules'
    quoted = json.dumps(label, ensure_ascii=False)
    for prompt in (
        rc.room_draft_prompt("src", room_label=label, block_range_text="r", message_count=1),
        rc.correction_prompt("draft", "src", room_label=label, scope="block r"),
        rc.era_room_prompt("sections", room_label=label, start_date="a", end_date="b"),
    ):
        assert f"Room: {quoted}." in prompt or f"room: {quoted}." in prompt
        assert label not in prompt  # the raw label never stands unquoted as prompt structure


@pytest.mark.parametrize("era_grows", [True, False])
def test_main_era_path_replaces_blocks_only_when_the_era_is_shorter(tmp_path, fit, monkeypatch, era_grows):
    """The era of the ordinary consolidation run is a compression (A6): a per-room
    era longer than the blocks it summarizes keeps those blocks, exactly as
    _compact_chronicle already requires."""
    from tests.test_consolidator_context_fit import _paths, _write_chat

    chat, blocks_path, meta_path = _paths(tmp_path)
    _write_chat(chat, count=c.BLOCK_SIZE, text_size=2)
    old = [{"range": f"2026-01-01 0{i}:00 - 0{i}:59", "message_count": 1, "content": f"block-{i} " + "x" * 40}
           for i in range(c.MAX_SUMMARY_BLOCKS)]
    blocks_path.parent.mkdir(parents=True, exist_ok=True)
    blocks_path.write_text(json.dumps(old), encoding="utf-8")
    run_len = sum(len(b["content"]) for b in old[:c.ERA_COMPRESS_COUNT])
    era_content = "e" * (run_len + 10 if era_grows else max(1, run_len // 4))
    seen = {}

    def fake_era(run, *_args, **_kwargs):
        seen["run"] = list(run)
        return {"range": "era", "message_count": len(run), "content": era_content, "era": True}, {}

    monkeypatch.setattr(c, "_compress_blocks_to_era", fake_era)
    assert c._run_block_consolidation(chat, blocks_path, meta_path, _LLM(), "", force_tail=True) is not None
    stored = json.loads(blocks_path.read_text(encoding="utf-8"))
    assert seen["run"] == old[:c.ERA_COMPRESS_COUNT]
    if era_grows:
        assert stored[:c.MAX_SUMMARY_BLOCKS] == old and not any(b.get("era") for b in stored)
    else:
        assert stored[0]["content"] == era_content and stored[1:c.MAX_SUMMARY_BLOCKS - c.ERA_COMPRESS_COUNT + 1] == old[c.ERA_COMPRESS_COUNT:]
    assert len(stored) == (c.MAX_SUMMARY_BLOCKS if era_grows else c.MAX_SUMMARY_BLOCKS - c.ERA_COMPRESS_COUNT + 1) + 1


def test_nominations_come_from_the_corrected_response_not_the_draft():
    """A false claim the correction removed from the memory cannot survive as a
    durable knowledge entry: only the CORRECTED response's block is released."""
    spans = []
    rows = [{"ts": f"2026-01-01T00:0{i}:00Z", "direction": "in", "text": f"entry-{i} plain", "chat_id": 1} for i in range(2)]
    text = c._format_entries_for_block(rows, include_room_labels=True, source_spans=spans)
    spans = [(start, end, note) for start, end, note in spans]

    class _Knowledge:
        def bind_entries(self, entries):
            return list(entries or [])

    prompts = []

    def call(prompt, label, *, fixed_prompt="", input_limit=None, call_type=""):
        prompts.append((label, prompt))
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0, "_consolidation_errors": []}
        if label == "Room summary":
            return ('draft memory\nKNOWLEDGE_ENTRIES_JSON: [{"topic":"leak","scope":"global","content":"owner approved"}]',
                    usage, _Knowledge())
        return ('corrected memory\nKNOWLEDGE_ENTRIES_JSON: [{"topic":"leak","scope":"global","content":"owner asked"},'
                ' {"topic":"invented","scope":"global","content":"never read"}]',
                usage, _Knowledge())

    content, usage = rc.summarize_source(
        call, text, spans,
        lambda part, note: rc.room_draft_prompt(part, room_label="Main", block_range_text="r", message_count=2, continuation_note=note),
        lambda draft, part, note: rc.correction_prompt(draft, part, room_label="Main", scope="block r", continuation_note=note),
    )
    assert content == "corrected memory"
    # The draft's proposed block reaches the correction...
    assert "KNOWLEDGE_ENTRIES_JSON" in prompts[1][1] and "owner approved" in prompts[1][1]
    # ...and only the corrected block is released: the draft's topic with the
    # corrected content, never an additional topic outside this correction's scope.
    assert [(e["topic"], e["content"]) for e in usage["_knowledge_entries"]] == [("leak", "owner asked")]


def test_presence_rows_of_one_room_share_one_label_from_transport_facts():
    from ouroboros.presence_bindings import conversation_key
    from ouroboros.presence_runner import _stable_numeric_id

    base = {"provider": "telegram", "account_id": "900", "conversation_id": "-100", "thread_id": ""}
    chat_id = _stable_numeric_id("presence-conversation", conversation_key("telegram", "900", "-100", ""))
    resolver = RoomLabelResolver(projects=[])
    inbound = {"chat_id": chat_id, "direction": "in", "transport": {**base, "conversation": {"title": "Aika ] admin"}}}
    receipt = {"chat_id": chat_id, "direction": "out", "type": "presence_delivery", "transport": {**base, "thread_id": "0"}}
    initiated = {"chat_id": chat_id, "direction": "in", "transport": dict(base)}
    summary = {"chat_id": chat_id, "direction": "system", "type": "task_summary",
               "presence_provenance": {**base, "binding_id": "1" * 32}}  # the turn's summary row carries no transport
    expected = f"Presence telegram -100 [chat_id={chat_id}]"
    # One room, four row types, one label: the correspondent-controlled title never enters it.
    assert {resolver.label(inbound), resolver.label(receipt), resolver.label(initiated), resolver.label(summary)} == {expected}
    assert resolver.label({**summary, "presence_provenance": {**base, "conversation_id": "-101"}}) == f"Unknown room [chat_id={chat_id}]"
    topic_chat = _stable_numeric_id("presence-conversation", conversation_key("telegram", "900", "-100", "42"))
    assert resolver.label({"chat_id": topic_chat, "transport": {**base, "thread_id": "42"}}) == (
        f"Presence telegram -100 topic 42 [chat_id={topic_chat}]"
    )
    # Transport facts that do not re-derive this exact chat id never name the room.
    assert resolver.label({"chat_id": chat_id + 1, "transport": dict(base)}) == f"Unknown room [chat_id={chat_id + 1}]"
    assert resolver.label({"chat_id": chat_id, "transport": {**base, "provider": ""}}) == f"Unknown room [chat_id={chat_id}]"
    # Brackets in a provider fact can never break the [room=...] marker.
    weird_chat = _stable_numeric_id("presence-conversation", conversation_key("telegram", "900", "x]y[z", ""))
    assert resolver.label({"chat_id": weird_chat, "transport": {**base, "conversation_id": "x]y[z"}}) == (
        f"Presence telegram x y z [chat_id={weird_chat}]"
    )


def test_terminal_projection_row_of_a_presence_turn_carries_the_room_facts(tmp_path):
    """The canonical terminal summary of a presence turn labels its room like every other row of it."""
    from ouroboros.presence_bindings import conversation_key
    from ouroboros.presence_runner import _stable_numeric_id
    from ouroboros.project_dialogue import append_terminal_task_projection
    from ouroboros.task_results import write_task_result

    chat_id = _stable_numeric_id("presence-conversation", conversation_key("telegram", "900", "-100", ""))
    event = {"provider": "telegram", "account_id": "900", "conversation_id": "-100", "thread_id": "",
             "source_event_id": "telegram:900:1", "conversation_key": "telegram:900:-100:0", "actor": {"id": "u1"}}
    write_task_result(tmp_path, "presence-turn-1", "completed", result="Done", terminal_origin="model_final",
                      metadata={"source": "presence", "presence": {"binding_id": "1" * 32, "event": event}})
    stored = __import__("ouroboros.task_results", fromlist=["load_task_result"]).load_task_result(tmp_path, "presence-turn-1")
    assert append_terminal_task_projection(tmp_path, "presence-turn-1", {"id": "presence-turn-1", "chat_id": chat_id},
                                           stored, {"status": "completed", "chat_id": chat_id})
    row = next(json.loads(line) for line in (tmp_path / "logs" / "chat.jsonl").read_text(encoding="utf-8").splitlines()
               if json.loads(line).get("type") == "task_summary")
    assert row["presence_provenance"]["conversation_id"] == "-100"
    assert RoomLabelResolver(projects=[]).label(row) == f"Presence telegram -100 [chat_id={chat_id}]"
    # A non-presence terminal row carries no presence facts at all.
    write_task_result(tmp_path, "plain-task", "completed", result="Done")
    plain = __import__("ouroboros.task_results", fromlist=["load_task_result"]).load_task_result(tmp_path, "plain-task")
    assert append_terminal_task_projection(tmp_path, "plain-task", {"id": "plain-task", "chat_id": 5}, plain,
                                           {"status": "completed", "chat_id": 5})
    rows = [json.loads(line) for line in (tmp_path / "logs" / "chat.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "presence_provenance" not in next(r for r in rows if r.get("task_id") == "plain-task")
