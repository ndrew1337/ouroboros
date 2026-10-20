"""Host entry for initial realtime setup, using the ordinary context and plugin owners.

The caller is an admitted host turn, with its env/memory/task/ToolContext. This
module opens no stores of its own and provides no public HTTP or model-tool door.
"""
from __future__ import annotations

import asyncio
import math
from typing import Any, Callable

from ouroboros.contracts.realtime import (
    RealtimeInput, RealtimeProbe, RealtimeSetupError, canonical_json, sha256,
)
from ouroboros.extension_registry_state import (
    _extensions, _lock, _realtime_providers, _unloading, extension_work_scope,
)


def input_from_context_plan(plan, *, model: str, selected_tools: list[dict],
                            output_allowance_tokens: int, messages: list[dict] | None = None) -> RealtimeInput:
    """Seal a complete ordinary projection; do not select a new mode, tools or window."""
    if plan.core is None or not plan.core.memory_view_json:
        raise RealtimeSetupError("missing_memory_view", "the ordinary host capture must include MemoryViewSnapshot")
    from ouroboros.memory_view import snapshot_from_json

    snapshot_from_json(plan.core.memory_view_json)  # malformed captures are not whole-view evidence
    if plan.initial_mode not in {"max", "low", "nano"}:
        raise RealtimeSetupError("context_mode", "the host must select an ordinary context mode")
    if plan.initial_mode == "nano" and plan.nano_projection is None:
        raise RealtimeSetupError("context_mode", "the selected Nano projection is missing")
    if plan.projection(plan.initial_mode).user_content_json is not None:
        # This is the ordinary fitter's mandatory original-input reader, not an
        # optional memory link. Setup has no tool loop to fulfill that obligation.
        raise RealtimeSetupError("task_input_reader_required", "setup cannot read the required original task input")
    if not isinstance(model, str) or not model.strip():
        raise RealtimeSetupError("model", "the host must select the provider model")
    if type(output_allowance_tokens) is not int or output_allowance_tokens < 1:
        raise RealtimeSetupError("output_allowance", "the host must supply its positive output allowance")
    return RealtimeInput(
        model=model, messages_json=canonical_json(
            plan.messages_for(plan.initial_mode) if messages is None else messages),
        tools_json=canonical_json(selected_tools), output_allowance_tokens=output_allowance_tokens,
        mode=plan.initial_mode, core_sha256=plan.core_sha256,
        memory_view_sha256=sha256(plan.core.memory_view_json),
    )


def capture_realtime_input(env, memory, task, *, ctx, model: str,
                           tool_schemas: list[dict], output_allowance_tokens: int,
                           review_context_builder=None) -> RealtimeInput:
    """Normal first-input capture for an admitted host turn, including its view receipt.

    The same builder and tool selector serve text. No voice-only Nano branch,
    independent memory reader, provider-window guess or output-allowance policy.
    A caller with an already prepared plan uses input_from_context_plan instead.
    """
    from ouroboros.context import build_llm_messages
    from ouroboros.tool_policy import select_tool_schemas

    if ctx is None:
        raise RealtimeSetupError("host_context", "the admitted turn's ToolContext is required")
    build_llm_messages(env, memory, task, review_context_builder, ctx, tool_schemas=tool_schemas)
    plan = ctx.context_fit_plan
    selected = select_tool_schemas(tool_schemas, context_mode=plan.initial_mode)
    return input_from_context_plan(plan, model=model, selected_tools=list(selected.schemas),
                                   output_allowance_tokens=output_allowance_tokens)


def loaded_provider(provider: str) -> dict:
    """A snapshot of a live provider's publication (handler, route, generation)."""
    with _lock:
        spec = _realtime_providers.get(provider)
        if spec is None or spec["skill"] in _unloading or spec["skill"] not in _extensions:
            raise RealtimeSetupError("provider_unavailable", provider)
        return dict(spec)


class _LoopWork:
    """Thread-safe Stop request; completion is the surrounding work scope's exit."""

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.task = None
        self.cancel_requested = False

    def cancel(self):
        self.loop.call_soon_threadsafe(self._cancel_once)

    def _cancel_once(self):
        if not self.cancel_requested:
            self.cancel_requested = True
            self.task.cancel()


async def run_realtime_setup(provider: str, selected: RealtimeInput, *, timeout_s: float,
                             observe: Callable[[str, str], Any], execute: bool = False,
                             expected_wire_sha256: str = "", probe: RealtimeProbe | None = None,
                             connect=None, expected_generation: str | None = None):
    """Call a published provider. Preview is offline; execution is an explicit host action.

    ``observe(direction, raw_json)`` retains the complete wire record before it is
    interpreted, and receives the provider's ``usage`` reports in host axes.
    ``connect`` is an optional host-supplied async context manager
    factory for offline transport tests; production omits it and uses the plugin's
    granted credential. No retries, tool dispatch, budget reservation or pricing
    are hidden here: those remain prerequisites owned by the admitted host caller.
    """
    if not isinstance(selected, RealtimeInput):
        raise RealtimeSetupError("input", "expected a sealed host RealtimeInput")
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise RealtimeSetupError("deadline", "timeout_s must be finite and positive")
    if not callable(observe):
        raise RealtimeSetupError("recorder", "a host event recorder is required")
    spec = loaded_provider(provider)
    if expected_generation is not None and spec["extension_generation"] != expected_generation:
        raise RealtimeSetupError("provider_changed", "preview's loaded provider revision is no longer current")
    # The lease covers the handler AND transport teardown, including slow
    # cancellation. A cancel request is not evidence that imports are unused.
    work = _LoopWork()

    async def invoke():
        with extension_work_scope(spec, work, retain_imports=True):
            observe("host", canonical_json({
                "type": "realtime.setup.dispatch", "provider": provider, "execute": execute,
                "extension_generation": spec["extension_generation"], "content_hash": spec["content_hash"],
                "input_sha256": selected.sha256,
            }))
            return await spec["handler"](
                selected, observe=observe, execute=execute,
                expected_wire_sha256=expected_wire_sha256, probe=probe, connect=connect,
            )

    work.task = asyncio.create_task(invoke())
    try:
        return await asyncio.wait_for(asyncio.shield(work.task), timeout=timeout_s)
    except BaseException:
        work.cancel()
        # Repeated Stop must not interrupt transport teardown and release the
        # executing imports early. Deadline stops work; it is not a teardown receipt.
        while not work.task.done():
            try:
                await asyncio.shield(work.task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not work.task.cancelled():
            work.task.exception()  # retrieve any terminal error; preserve the original refusal/Stop
        raise
