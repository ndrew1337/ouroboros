"""knowledge_list(topics=...) is the read SYSTEM.md points at when the index shows only its outline."""
from __future__ import annotations

import pathlib

from ouroboros.tools.knowledge import _knowledge_list, get_tools


class _Ctx:
    def __init__(self, drive_root):
        self.drive_root = pathlib.Path(drive_root)
        self.project_id = ""
        self.task_id = "t1"

    def drive_path(self, rel):
        return self.drive_root / rel


def _shelf(tmp_path):
    ctx = _Ctx(tmp_path / "drive")
    kdir = ctx.drive_root / "memory" / "knowledge"
    (kdir / "people").mkdir(parents=True)
    (kdir / "git-recipes.md").write_text(
        "---\ntype: procedure\nsummary: Use rebase sparingly.\n---\n# Git recipes\n", encoding="utf-8")
    (kdir / "people" / "elena.md").write_text(
        "---\ntype: person\nsummary: Prefers short calls.\n---\n# Elena\n", encoding="utf-8")
    return ctx


def test_topics_narrow_the_listing_and_an_omitted_list_keeps_the_whole_shelf(tmp_path):
    ctx = _shelf(tmp_path)
    whole = _knowledge_list(ctx)
    assert "rebase" in whole and "short calls" in whole
    narrowed = _knowledge_list(ctx, topics=["people/"])
    assert "short calls" in narrowed and "rebase" not in narrowed
    exact = _knowledge_list(ctx, topics=["git-recipes"])
    assert "rebase" in exact and "short calls" not in exact


def test_topics_must_be_a_list_of_strings(tmp_path):
    ctx = _shelf(tmp_path)
    assert "TOOL_ARG_ERROR" in _knowledge_list(ctx, topics="people/")
    assert "TOOL_ARG_ERROR" in _knowledge_list(ctx, topics=[3])


def test_the_tool_schema_offers_topics():
    entry = next(tool for tool in get_tools() if tool.name == "knowledge_list")
    properties = entry.schema["parameters"]["properties"]
    assert properties["topics"]["type"] == "array"
    assert "topics" not in entry.schema["parameters"]["required"]
