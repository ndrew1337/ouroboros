"""What a tool says about an argument it did not obey (DEVELOPMENT "LLM-first affordances").

Models fill every key of a tool schema. A value that asks for nothing takes the
omitted path and the result says so in one line; a value that asks for something
the call cannot serve is refused ONCE, typed, naming the field, the value received
and the repair — a refusal that only restates the rule is retried unchanged.
"""

from __future__ import annotations

from typing import Any, Iterable

from ouroboros.tools.tool_result import ToolResult, _publish_tool_result


def ignored_argument_note(name: str, value: Any, why: str) -> str:
    """One result line for an argument that took the omitted path."""
    return f"{name}={value!r} ignored: {why}"


def argument_refusal(
    ctx: Any, identifier: str, problems: Iterable[str], *, effect: str = "",
) -> str:
    """Publish one typed refusal naming every violated constraint (the W2 shape).

    ``identifier`` stays the first-line marker so each domain keeps its own code
    in the text; the typed ``TOOL_ARG_ERROR`` is what status, reflection and the
    Pattern Register read. ``effect`` states what the refused call did NOT do.
    """
    text = f"⚠️ {identifier}: " + "; ".join(str(item).rstrip(". ") for item in problems) + "."
    if effect:
        text += f" {effect}"
    return _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR", text=text))
