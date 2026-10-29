"""#918: scratchpad blocks stay until the integrating mind folds them (owner, 29 Oct 2026: «А — блоки, свёртку принимаю я»).

Real consumers end to end: the ``update_scratchpad`` tool through ``ToolRegistry`` dispatch,
the canonical ``memory/scratchpad_blocks.json`` and ``memory/scratchpad_journal.jsonl`` owned by
``Memory``, and the next task's actual first request (``context.build_llm_messages``). Nothing
leaves the active list by count, size, age or a post-task Light pass; a fold replaces exactly the
blocks it names (their sha256 as the next context shows them), after the exact originals are
journaled, and a failed, empty, stale or ambiguous fold leaves every active block in place.
"""
from __future__ import annotations

import hashlib
import json
import re

import pytest

from ouroboros.memory import Memory
from ouroboros.tools.registry import ToolContext, ToolRegistry

_DIGEST = re.compile(r"^### \[[^\]]*\] sha256 ([0-9a-f]{64})$", re.M)


@pytest.fixture
def install(tmp_path):
    from tests.test_cache_optimization import _make_env_and_memory

    env, memory = _make_env_and_memory(tmp_path)
    # The shared fixture seeds a flat pre-block scratchpad; this installation keeps blocks.
    memory.scratchpad_path().unlink()
    memory.ensure_files()
    registry = ToolRegistry(repo_dir=env.repo_dir, drive_root=env.drive_root)
    registry.set_context(ToolContext(repo_dir=env.repo_dir, drive_root=env.drive_root, task_id="root-918"))
    return env, memory, registry


def _tool(registry, **args):
    return registry.execute_result("update_scratchpad", args)


def _blocks(memory):
    return json.loads(memory.scratchpad_blocks_path().read_text(encoding="utf-8"))


def _journal(memory, kind=None):
    path = memory.journal_path()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []
    return [row for row in rows if kind is None or row.get("type") == kind]


def _next_request(env, memory) -> str:
    """The changing tail of the next task's real first request, where the scratchpad lives."""
    from ouroboros.context import build_llm_messages

    messages, _cap = build_llm_messages(env=env, memory=memory, task={"id": "next-918", "type": "task", "text": "hi"})
    return messages[0]["content"][-1]["text"]


def _scratchpad_section(text: str) -> str:
    start = text.index("## Scratchpad (from")
    following = [match.start() for match in re.finditer(r"\n## (?!Scratchpad \(working memory)", text[start + 1:])]
    return text[start:start + 1 + following[0]] if following else text[start:]


def _digests_in_view(env, memory):
    return _DIGEST.findall(_scratchpad_section(_next_request(env, memory)))


def _canonical_sha(block) -> str:
    return hashlib.sha256(json.dumps(block, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def scratchpad_block_sha256(block) -> str:
    from ouroboros.memory import scratchpad_block_sha256 as owner

    return owner(block)


def _note(index: int, size: int = 0) -> str:
    return f"NOTE-{index:02d} " + ("n" * size if size else f"working thought number {index}")


# --- nothing rotates out ------------------------------------------------------------------


def test_twelve_small_appends_all_stay_in_storage_and_in_the_next_request(install):
    env, memory, registry = install
    for index in range(12):
        result = _tool(registry, content=_note(index))
        assert result.status == "ok", result.text
    blocks = _blocks(memory)
    assert [block["content"] for block in blocks] == [_note(index) for index in range(12)]
    assert _journal(memory, "block_evicted") == []
    section = _scratchpad_section(_next_request(env, memory))
    assert all(_note(index) in section for index in range(12))
    assert "12 blocks" in section and "/10" not in section
    assert "PARTIAL" not in section and "budget gap" not in section


def test_content_above_sixty_thousand_characters_stays_active(install):
    env, memory, registry = install
    for index in range(3):
        assert _tool(registry, content=_note(index, 25_000)).status == "ok"
    assert [len(block["content"]) for block in _blocks(memory)] == [len(_note(index, 25_000)) for index in range(3)]
    assert _journal(memory, "block_evicted") == []


def test_a_section_above_ninety_thousand_characters_reaches_the_next_request_whole(install):
    env, memory, registry = install
    for index in range(4):
        assert _tool(registry, content=_note(index, 25_000)).status == "ok"
    assert len(memory.load_scratchpad()) > 90_000
    section = _scratchpad_section(_next_request(env, memory))
    assert all(_note(index, 25_000) in section for index in range(4))
    assert "budget gap" not in section and "PARTIAL" not in section and "omitted" not in section
    assert "already loaded" in section.split("\n", 1)[0]


def test_each_block_shows_the_sha256_of_its_exact_stored_json(install):
    env, memory, registry = install
    for index in range(3):
        _tool(registry, content=_note(index))
    stored = _blocks(memory)
    assert [scratchpad_block_sha256(block) for block in stored] == [_canonical_sha(block) for block in stored]
    # Newest first, as the model has always read its working memory.
    assert _digests_in_view(env, memory) == [_canonical_sha(block) for block in reversed(stored)]


def test_the_tool_schema_offers_the_fold_and_promises_no_eviction(install):
    _env, _memory, registry = install
    schema = registry.get_schema_by_name("update_scratchpad")
    function = schema.get("function", schema)
    properties = function["parameters"]["properties"]
    assert properties["replaces"]["type"] == "array" and properties["replaces"]["items"]["type"] == "string"
    assert properties["draft_source"]["type"] == "string"
    assert function["parameters"]["required"] == ["content"]
    description = function["description"].lower()
    assert "evict" not in description and "10 blocks" not in description and "60000" not in description
    assert "replaces" in description


# --- the fold ----------------------------------------------------------------------------


def test_a_fold_replaces_exactly_the_named_blocks_after_journaling_their_exact_originals(install):
    env, memory, registry = install
    for index in range(4):
        _tool(registry, content=_note(index))
    before = _blocks(memory)
    newest_first = _digests_in_view(env, memory)
    chosen = [newest_first[-1], newest_first[-2]]  # the two oldest, as the mind read them

    result = _tool(registry, content="FOLD-A: notes 0 and 1 mean one decision still open.", replaces=chosen)

    assert result.status == "ok", result.text
    after = _blocks(memory)
    assert after[1:] == before[2:]  # unnamed blocks: same bytes, same order
    fold = after[0]
    assert fold["source"] == "fold" and fold["content"].startswith("FOLD-A")
    meta = fold["metadata"]
    assert meta["fold"]["replaces"] == chosen
    assert meta["fold"]["covers"] == {"from": before[0]["ts"], "to": before[1]["ts"], "blocks": 2}
    assert meta["focus"]["task_id"] == "root-918" and "model" not in json.dumps(meta)
    entry_id = meta["source_ref"]["entry_id"]
    [preserved] = _journal(memory, "fold_sources_preserved")
    [completed] = _journal(memory, "blocks_folded")
    assert preserved["entry_id"] == completed["entry_id"] == entry_id
    assert preserved["source_blocks"] == before[:2]  # exact originals, metadata included
    assert preserved["source_sha256"] == chosen
    kinds = [row["type"] for row in _journal(memory)]
    assert kinds.index("fold_sources_preserved") < kinds.index("blocks_folded")
    assert completed["content_len"] == sum(len(block["content"]) for block in after)
    assert entry_id in result.text and "2 blocks" in result.text

    section = _scratchpad_section(_next_request(env, memory))
    assert "FOLD-A" in section and _note(0) not in section and _note(1) not in section
    assert _note(2) in section and _note(3) in section
    assert f"entry_id={entry_id}" in section


def test_the_preservation_entry_id_names_one_event_not_only_its_sources(install, monkeypatch):
    env, memory, registry = install
    for index in range(2):
        _tool(registry, content=_note(index))
    chosen = _digests_in_view(env, memory)
    from ouroboros import memory as memory_module

    real_write, attempts = memory_module.atomic_write_json, []

    def fail_first_canonical_write(*args, **kwargs):
        attempts.append(args[0])
        if len(attempts) == 1:
            raise OSError("disk full")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(memory_module, "atomic_write_json", fail_first_canonical_write)
    failed = _tool(registry, content="FOLD attempt one, different words.", replaces=chosen)
    assert failed.status != "ok" and "disk full" in failed.text
    retried = _tool(registry, content="FOLD attempt two, the accepted words.", replaces=chosen)
    assert retried.status == "ok", retried.text
    preserved = _journal(memory, "fold_sources_preserved")
    assert len(preserved) == 2 and preserved[0]["source_blocks"] == preserved[1]["source_blocks"]
    assert preserved[0]["entry_id"] != preserved[1]["entry_id"]
    assert _blocks(memory)[0]["metadata"]["source_ref"]["entry_id"] == preserved[1]["entry_id"]
    [failure] = _journal(memory, "block_fold_failed")
    assert failure["entry_id"] == preserved[0]["entry_id"] and "disk full" in failure["error"]


def test_a_fold_over_an_earlier_fold_keeps_every_original_resolvable(install):
    env, memory, registry = install
    for index in range(4):
        _tool(registry, content=_note(index))
    originals = _blocks(memory)
    newest_first = _digests_in_view(env, memory)
    assert _tool(registry, content="FOLD-1 carries notes 0 and 1.", replaces=newest_first[-2:][::-1]).status == "ok"
    _tool(registry, content=_note(4))
    first_fold = _blocks(memory)[0]
    view = _digests_in_view(env, memory)
    fold_sha = scratchpad_block_sha256(first_fold)
    assert fold_sha in view
    note2_sha = scratchpad_block_sha256(originals[2])

    result = _tool(registry, content="FOLD-2 carries FOLD-1 and note 2 forward.", replaces=[fold_sha, note2_sha])

    assert result.status == "ok", result.text
    after = _blocks(memory)
    assert [block["content"] for block in after] == [
        "FOLD-2 carries FOLD-1 and note 2 forward.", _note(3), _note(4)]
    # Resolve the chain through the journal alone: FOLD-2 -> FOLD-1 (+note 2) -> notes 0 and 1.
    rows = {row["entry_id"]: row for row in _journal(memory, "fold_sources_preserved")}
    second = rows[after[0]["metadata"]["source_ref"]["entry_id"]]
    assert second["source_blocks"] == [first_fold, originals[2]]
    first = rows[second["source_blocks"][0]["metadata"]["source_ref"]["entry_id"]]
    assert first["source_blocks"] == originals[:2]


def test_a_block_appended_after_the_mind_read_its_context_survives_the_fold(install):
    env, memory, registry = install
    for index in range(3):
        _tool(registry, content=_note(index))
    chosen = _digests_in_view(env, memory)[-2:]  # read by the folding focus
    concurrent = Memory(memory.drive_root).append_scratchpad_block("CONCURRENT from another room", source="task")

    assert _tool(registry, content="FOLD of notes 0 and 1.", replaces=chosen).status == "ok"

    after = _blocks(memory)
    assert [block["content"] for block in after] == ["FOLD of notes 0 and 1.", _note(2), "CONCURRENT from another room"]
    assert after[-1] == concurrent


@pytest.mark.parametrize("change_same_source", [False, True])
def test_a_concurrent_change_or_append_is_never_lost(install, change_same_source):
    """The retired Light pass's prefix check, now at the fold: a block another writer changed after
    the mind read it is a conflict that keeps the newer bytes; an appended block survives the fold."""
    env, memory, registry = install
    original = {"ts": "2026-10-01T10:00:00+00:00", "source": "task", "content": "old " * 2000}
    memory.mutate_scratchpad_blocks(lambda _rows: [original])
    [read] = _digests_in_view(env, memory)
    newer = {**original, "content": "new concurrent content"}
    appended = {"ts": "2026-10-01T10:05:00+00:00", "source": "task", "content": "A new episode"}
    memory.mutate_scratchpad_blocks(lambda rows: [newer] if change_same_source else rows + [appended])

    result = _tool(registry, content="FOLD of what the mind read earlier.", replaces=[read])

    if change_same_source:
        assert result.status != "ok" and "SCRATCHPAD_FOLD_CONFLICT" in result.text
        assert _blocks(memory) == [newer]
    else:
        assert result.status == "ok", result.text
        assert [block["content"] for block in _blocks(memory)] == ["FOLD of what the mind read earlier.", "A new episode"]
        assert _blocks(memory)[1] == appended


def test_a_stale_or_unknown_sha256_is_a_conflict_and_changes_nothing(install):
    env, memory, registry = install
    for index in range(3):
        _tool(registry, content=_note(index))
    oldest = _digests_in_view(env, memory)[-1]
    assert _tool(registry, content="FOLD by the first focus.", replaces=[oldest]).status == "ok"
    before_bytes = memory.scratchpad_blocks_path().read_bytes()
    preserved = len(_journal(memory, "fold_sources_preserved"))

    for stale in ([oldest], ["0" * 64], [oldest[:12]]):
        refused = _tool(registry, content="FOLD by a second focus that read an older view.", replaces=stale)
        assert refused.status != "ok" and "SCRATCHPAD_FOLD_CONFLICT" in refused.text
        assert memory.scratchpad_blocks_path().read_bytes() == before_bytes
    assert len(_journal(memory, "fold_sources_preserved")) == preserved
    refusals = _journal(memory, "block_fold_refused")
    assert len(refusals) == 3 and refusals[0]["fold_block"]["content"].startswith("FOLD by a second focus")
    # The refusal names what is active now, so the mind can read and choose again.
    assert all(digest in refused.text for digest in _digests_in_view(env, memory))


def test_identical_blocks_fold_only_when_every_copy_is_selected(install):
    env, memory, registry = install
    twin = {"ts": "2026-10-01T10:00:00+00:00", "source": "task", "content": "TWIN legacy note"}
    memory.mutate_scratchpad_blocks(lambda _rows: [dict(twin), dict(twin), {**twin, "content": "OTHER"}])
    digest = scratchpad_block_sha256(twin)
    before_bytes = memory.scratchpad_blocks_path().read_bytes()

    ambiguous = _tool(registry, content="FOLD of one twin is ambiguous.", replaces=[digest])
    assert ambiguous.status != "ok" and "2 active blocks" in ambiguous.text
    assert memory.scratchpad_blocks_path().read_bytes() == before_bytes

    both = _tool(registry, content="FOLD of both twins, named twice.", replaces=[digest, digest])
    assert both.status == "ok", both.text
    assert [block["content"] for block in _blocks(memory)] == ["FOLD of both twins, named twice.", "OTHER"]
    [preserved] = _journal(memory, "fold_sources_preserved")
    assert preserved["source_blocks"] == [twin, twin]


def test_an_empty_fold_changes_nothing(install):
    env, memory, registry = install
    for index in range(2):
        _tool(registry, content=_note(index))
    before_bytes = memory.scratchpad_blocks_path().read_bytes()
    chosen = _digests_in_view(env, memory)
    for content in ("", "   ", "short"):
        refused = _tool(registry, content=content, replaces=chosen)
        assert refused.status != "ok" and refused.code == "TOOL_ARG_ERROR"
    assert memory.scratchpad_blocks_path().read_bytes() == before_bytes
    assert _journal(memory, "fold_sources_preserved") == []


def test_a_helper_draft_is_accepted_through_the_same_fold_with_its_source(install):
    env, memory, registry = install
    for index in range(2):
        _tool(registry, content=_note(index))
    chosen = _digests_in_view(env, memory)
    result = _tool(registry, content="FOLD drafted by a helper, accepted as written.", replaces=chosen,
                   draft_source="helper task c0ffee: Light draft of notes 0 and 1")
    assert result.status == "ok", result.text
    fold = _blocks(memory)[0]
    assert fold["metadata"]["fold"]["draft_source"] == "helper task c0ffee: Light draft of notes 0 and 1"
    assert fold["metadata"]["focus"]["task_id"] == "root-918"  # the accepting focus, not the helper
    section = _scratchpad_section(_next_request(env, memory))
    assert "helper draft: helper task c0ffee" in section


# --- failure and crash points ------------------------------------------------------------


def test_a_failed_preservation_write_leaves_every_active_block(install, monkeypatch):
    env, memory, registry = install
    for index in range(3):
        _tool(registry, content=_note(index))
    chosen = _digests_in_view(env, memory)[-2:]
    before_bytes = memory.scratchpad_blocks_path().read_bytes()
    before_md = memory.scratchpad_path().read_bytes()
    from ouroboros import memory as memory_module

    real = memory_module.append_jsonl
    monkeypatch.setattr(memory_module, "append_jsonl", lambda path, row, *a, **k: (
        False if row.get("type") == "fold_sources_preserved" else real(path, row, *a, **k)))

    refused = _tool(registry, content="FOLD whose sources could not be preserved.", replaces=chosen)

    assert refused.status != "ok" and "journal" in refused.text.lower()
    assert memory.scratchpad_blocks_path().read_bytes() == before_bytes
    assert memory.scratchpad_path().read_bytes() == before_md
    section = _scratchpad_section(_next_request(env, memory))
    assert all(_note(index) in section for index in range(3))


def test_a_failed_canonical_write_after_preservation_keeps_the_active_blocks_and_says_so(install, monkeypatch):
    env, memory, registry = install
    for index in range(3):
        _tool(registry, content=_note(index))
    chosen = _digests_in_view(env, memory)[-2:]
    before_bytes = memory.scratchpad_blocks_path().read_bytes()
    from ouroboros import memory as memory_module

    def read_only_volume(*_args, **_kwargs):
        raise OSError("read-only volume")

    monkeypatch.setattr(memory_module, "atomic_write_json", read_only_volume)

    failed = _tool(registry, content="FOLD that could not be published.", replaces=chosen)

    assert failed.status != "ok" and "read-only volume" in failed.text
    monkeypatch.undo()
    assert memory.scratchpad_blocks_path().read_bytes() == before_bytes
    [preserved] = _journal(memory, "fold_sources_preserved")
    [failure] = _journal(memory, "block_fold_failed")
    assert failure["entry_id"] == preserved["entry_id"] and _journal(memory, "blocks_folded") == []
    section = _scratchpad_section(_next_request(env, memory))
    assert all(_note(index) in section for index in range(3)) and "FOLD that could not" not in section


def test_a_projection_failure_after_the_canonical_fold_reports_the_applied_fold(install, monkeypatch):
    env, memory, registry = install
    for index in range(3):
        _tool(registry, content=_note(index))
    chosen = _digests_in_view(env, memory)[-2:]
    stale_md = memory.scratchpad_path().read_bytes()

    def broken_projection(self, blocks):
        raise OSError("projection disk error")

    monkeypatch.setattr(Memory, "_write_scratchpad_markdown", broken_projection)
    result = _tool(registry, content="FOLD applied while the projection failed.", replaces=chosen)

    assert result.status == "ok", result.text  # the canonical change happened: no false failure
    assert "projection" in result.text and "projection disk error" in result.text
    assert _blocks(memory)[0]["content"] == "FOLD applied while the projection failed."
    assert memory.scratchpad_path().read_bytes() == stale_md  # the projection really is stale
    [completed] = _journal(memory, "blocks_folded")
    assert "projection disk error" in completed["projection_error"]
    section = _scratchpad_section(_next_request(env, memory))
    # The next request reads the canonical blocks, never the stale projection.
    assert "FOLD applied while the projection failed." in section
    assert _note(0) not in section and _note(2) in section


def test_a_projection_failure_on_append_does_not_hide_the_new_block(install, monkeypatch):
    env, memory, registry = install
    _tool(registry, content=_note(0))

    def broken_projection(self, blocks):
        raise OSError("projection disk error")

    monkeypatch.setattr(Memory, "_write_scratchpad_markdown", broken_projection)
    result = _tool(registry, content=_note(1))

    assert result.status == "ok" and "projection disk error" in result.text
    assert [block["content"] for block in _blocks(memory)] == [_note(0), _note(1)]
    assert _journal(memory, "block_append_failed") == []
    assert _note(1) in _scratchpad_section(_next_request(env, memory))


# --- no automatic writer remains ----------------------------------------------------------


def test_no_count_or_size_cap_remains():
    """The former 10-block and 60 000-character caps (the automatic writer's own removal is pinned
    in ``test_scratchpad_consolidation``)."""
    from ouroboros import context_budget
    from ouroboros import memory as memory_module

    assert not hasattr(memory_module, "_SCRATCHPAD_MAX_BLOCKS")
    assert not hasattr(context_budget, "SCRATCHPAD_MAX_CONTENT_CHARS")
