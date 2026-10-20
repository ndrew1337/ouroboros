"""The ordinary actor's host port for a loaded Live Conversation feature.

The feature moves events; this host port owns fitting, tool execution, canonical
history, spending and Stop. Neither wire packets nor durable task metadata can
mint this process-local turn. Every loaded generation is retained through close.
"""
from __future__ import annotations

import asyncio
import copy
import functools
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict

from ouroboros.contracts.live_conversation import LiveOptions
from ouroboros.contracts.realtime import RealtimeSetupError, canonical_json, sha256
from ouroboros.realtime_setup import _LoopWork, loaded_provider
from ouroboros.realtime_session import joined
from ouroboros.realtime_turn import RealtimeTurn, _positive_number


def loaded_feature(key):
    from ouroboros.extension_registry_state import _conversation_features, _extensions, _lock, _unloading

    with _lock:
        spec = _conversation_features.get(key)
        if spec is None or spec["skill"] in _unloading or spec["skill"] not in _extensions:
            raise RealtimeSetupError("feature_unavailable", str(key))
        return {**spec, "key": key}


@contextmanager
def feature_configuration(key):
    """A configure callback is loaded code too; guard its complete invocation."""
    from ouroboros.extension_registry_state import extension_work_scope

    spec = loaded_feature(key)

    class Read:
        def cancel(self):
            pass  # synchronous reader; imports remain leased until its return

    with extension_work_scope(spec, Read(), retain_imports=True):
        yield spec, spec["configure"]()


class LiveConversationTurn(RealtimeTurn):
    accepts_owner_messages = True

    def __init__(self, feature, config, spec, *, publish):
        self.feature, self.spec, self.publish = feature, spec, publish
        self.provider = config["provider"]
        provider = loaded_provider(self.provider)
        if not callable(provider.get("session")):
            raise RealtimeSetupError("provider_unavailable", "selected provider has no live session")
        self.supports_images = getattr(provider["session"], "supports_images", False) is True
        self.route, self._generation = provider["route"], provider["extension_generation"]
        self._content_hash = provider["content_hash"]
        self.native_model, self.model = spec.model, provider["route"].model_prefix + spec.model
        self.timeout_s = _positive_number(config["max_seconds"], "deadline")
        self.reservation_usd = _positive_number(config["reservation_usd"], "funding")
        self.deadline = time.monotonic() + self.timeout_s
        self.request = {"kind": "live_conversation", "feature": feature["key"], "provider": self.provider,
                        "model": spec.model, "call_id": spec.call_id}
        self._lock, self._closed = threading.Lock(), False

    def authorize(self, *_args):
        raise RealtimeSetupError("funding", "call funding belongs to its admitted feature configuration")

    def run(self, ctx, messages, tools, *, task, initial_effort, incoming_messages=None, event_queue=None):
        from ouroboros.call_peers import close_hub, get_hub
        from ouroboros.extension_registry_state import extension_work_scope
        from ouroboros.usage_accounting import BudgetExceeded

        hub = get_hub(self.spec.call_id)
        if hub is None:
            raise RealtimeSetupError("call_not_found", "this call's process-local hub ended")
        host = None

        async def owned():
            work = _LoopWork()
            work.task = asyncio.current_task()
            # Feature unload must retain its driver imports through all socket,
            # history and tool teardown, as provider unload does for its adapter.
            with extension_work_scope(self.feature, work, retain_imports=True):
                runner = asyncio.create_task(self.feature["handler"](host))
                controls = asyncio.create_task(host.watch_controls())
                try:
                    done, _ = await asyncio.wait((runner, controls), return_when=asyncio.FIRST_COMPLETED)
                    if controls in done:
                        await controls
                    return await runner
                finally:
                    for child in (runner, controls):
                        if not child.done():
                            child.cancel()
                    for child in (runner, controls):
                        try:
                            await joined(child)
                        except (asyncio.CancelledError, Exception):
                            pass
                    # join any already-started tool invocation; cancellation of
                    # its await cannot make an executing effect disappear.
                    await host.finish()

        reason, status = "finished", "ok"
        try:
            presence = (task.get("metadata") or {}).get("presence") or {}
            admitted_presence = (self.spec.opened_by == "skill"
                                 and presence.get("binding_id") == self.spec.presence_binding_id)
            if not ctx.is_direct_chat or not (task.get("origin_message_ref") or admitted_presence):
                raise RealtimeSetupError("owner_ingress", "call requires a canonical admitted owner turn")
            host = ConversationHost(self, ctx, messages, tools, task, initial_effort, hub)
            host.incoming, host.event_queue = incoming_messages, event_queue
            asyncio.run(owned())
        except BudgetExceeded:
            reason, status = "budget_exhausted", "failed"
            raise
        except (Exception, asyncio.CancelledError) as exc:
            reason = getattr(exc, "code", "") or ("cancelled" if isinstance(exc, asyncio.CancelledError) else "call_failed")
            status = "cancelled" if reason in {"cancelled", "finalize_requested", "owner_stop"} else "infra_failed"
            if host is not None:
                host.observe("host", canonical_json({"type": "call.ended", "reason": reason}))
        finally:
            with self._lock:
                self._closed = True
            close_hub(self.spec.call_id, reason)
            self.publish({"type": "conversation_finished", "chat_id": self.spec.chat_id,
                          "call_id": self.spec.call_id, "task_id": ctx.task_id, "reason": reason})
        return "Call ended. Its transcript is in this room.", {
            "terminal_origin": "host_notice", "execution_status": status, "reason_code": reason,
        }, host.trace if host is not None else {"reasoning_notes": [], "tool_calls": []}


class ConversationHost:
    """Port given only to an admitted feature; all effects use ordinary owners."""

    def __init__(self, turn, ctx, messages, tools, task, effort, hub):
        from ouroboros.artifacts import task_artifact_dir_path
        from ouroboros.loop import _reset_turn_state, _setup_dynamic_tools, StatefulToolExecutor
        from ouroboros.tool_policy import initial_tool_schemas

        self.turn, self.ctx, self.messages, self.tools = turn, ctx, messages, tools
        self.task, self.effort, self.hub = task, effort, hub
        self.trace = {"reasoning_notes": [], "tool_calls": []}
        self.plan = ctx.context_fit_plan
        if self.plan.model != turn.model or self.plan.provider != turn.route.accounting_provider:
            raise RealtimeSetupError("route", "call input must use the selected realtime route")
        self.schemas = initial_tool_schemas(tools, context_mode=self.plan.initial_mode)
        _reset_turn_state(ctx)
        self.schemas, _ = _setup_dynamic_tools(tools, self.schemas, messages, context_mode=self.plan.initial_mode)
        ctx.messages, ctx.active_context_mode, ctx.active_effort = messages, self.plan.initial_mode, effort
        ctx._execution_trace = self.trace
        self.executor = StatefulToolExecutor()
        self.tool_jobs = set()
        self.executed, self.captioned = {}, set()
        self.wire_path = task_artifact_dir_path(ctx.drive_root, ctx.task_id, create=True) / "conversation-wire.jsonl"
        self.connection_number = 0
        self._tools_lock = asyncio.Lock()
        self._rows_lock = asyncio.Lock()
        self.incoming = self.event_queue = None
        self.owner_seen = set()
        self.usage = {}
        self.rounds = 0

    def checkpoint(self, boundary):
        from types import SimpleNamespace
        from ouroboros.working_checkpoint import save_round, flush_content_acks

        saved = save_round(SimpleNamespace(tools=self.tools, messages=self.messages, llm_trace=self.trace,
            accumulated_usage=self.usage, round_idx=self.rounds, tool_schemas=self.schemas,
            owner_msg_seen=self.owner_seen), boundary)
        if saved:
            flush_content_acks(self.ctx)

    async def mailbox(self):
        """Use the ordinary typed drain; checkpoint before acknowledging content."""
        from ouroboros.loop_round_limits import _drain_incoming_messages

        if self.incoming is None:
            return []
        async with self._tools_lock:
            rows = []
            def drain():
                # A separate tail prevents merging new words into an item that
                # the provider has already accepted. Authority still comes from
                # the ordinary mailbox drain, including its relays and controls.
                controls = _drain_incoming_messages(rows, self.incoming, self.ctx.drive_root,
                    self.ctx.task_id, self.event_queue, self.owner_seen, owner_ctx=self.ctx,
                    defer_content_ack=True)
                self.messages.extend(rows)
                if rows:
                    self.selected_input()
                    self.checkpoint("ready")
                return controls
            controls = await joined(asyncio.to_thread(drain))
            if controls.get("finalize_now") or controls.get("owner_stop"):
                raise RealtimeSetupError("owner_stop", "ordinary owner control ended the call")
            return rows

    def observe(self, direction, raw):
        with self.wire_path.open("a", encoding="utf-8") as stream:
            stream.write(canonical_json({"connection": self.connection_number, "direction": direction, "raw": raw}) + "\n")
            stream.flush()

    async def check(self):
        waiter = self.ctx.model_wait_context
        if waiter is None:
            raise RealtimeSetupError("host_control", "call has no ordinary model-wait owner")
        reason = await asyncio.to_thread(waiter.control_reason, strict_current=True)
        if reason:
            raise RealtimeSetupError(reason, "ordinary task control ended the call")
        if time.monotonic() >= self.turn.deadline:
            raise RealtimeSetupError("deadline", "call reached its configured time limit")

    async def watch_controls(self):
        while True:
            await self.check()
            await asyncio.sleep(.05)

    def selected_input(self):
        """Measure ordinary selected bytes without rewriting or refusing a prediction.

        The provider's actual output bound is not available yet. Physical fit
        belongs to the complete AttemptRequest, not this ordinary reply floor.
        """
        from ouroboros.context_fit import measure_main_fit
        from ouroboros.realtime_setup import input_from_context_plan

        fit = measure_main_fit(self.plan, self.messages, self.schemas, profile="owner_" + self.plan.initial_mode,
                               rendered_mode=self.plan.initial_mode, round_id=self.ctx.task_id + ":call",
                               reasoning_effort=self.effort)
        return input_from_context_plan(self.plan, model=self.turn.native_model, selected_tools=self.schemas,
            output_allowance_tokens=fit.measurement.reply_allowance_tokens, messages=self.messages,
            tool_reader=True), fit

    async def session(self, consume):
        """One physical connection, reserved and sealed before any provider dispatch."""
        from ouroboros.extension_registry_state import extension_work_scope
        from ouroboros.loop_model_call import _physical_context_for_fit
        from ouroboros.observability import persist_physical_candidate
        from ouroboros.response_limits import _response_fingerprint, positive_limit, resolve_response_limit
        from ouroboros.usage_accounting import AttemptRequest, execute_physical_attempt_async

        await self.check()
        selected, fit = self.selected_input()
        provider = loaded_provider(self.turn.provider)
        if provider["extension_generation"] != self.turn._generation:
            raise RealtimeSetupError("provider_changed", "selected loaded generation changed; start a new call")
        route = dict(provider=self.turn.route.accounting_provider, model=self.turn.model,
                     base_url=self.turn.route.endpoint)
        limit = resolve_response_limit(self.ctx.drive_root, **route, allow_fetch=False)
        model_bound = None
        if (not limit.stale and limit.observed_at and limit.status in {"confirmed", "asserted"}
                and limit.route_fp == _response_fingerprint(**route)):
            model_bound = positive_limit(limit.max_output_tokens) or None
        work = _LoopWork()
        work.task = asyncio.current_task()
        with extension_work_scope(provider, work, retain_imports=True):
            self.connection_number += 1
            session = provider["session"](selected, LiveOptions(voice=self.turn.spec.voice,
                model_output_bound_tokens=model_bound), observe=self.observe)
            output_bound = session.output_bound_tokens
            if output_bound is not None and (type(output_bound) is not int or output_bound < 1):
                raise RealtimeSetupError("output_bound", "provider output bound must be a positive integer or None")
            # Keep ordinary selection/fit intact. The physical request carries
            # the actual native bound, even above its allowance; remeasuring a
            # Nano plan would count its reply floor instead of that bound.
            output_reserve = output_bound if output_bound is not None else selected.output_allowance_tokens
            wire = canonical_json(session.wire)
            # Audio tariffs are not text tariffs. Reserve the configured exposure;
            # absent authoritative modality pricing remains unknown, never zero.
            usage = {"cost_invalid": True, "cost_evidence": {"kind": "realtime_modalities", "responses": []}}
            request = AttemptRequest(model=self.turn.model, provider=self.turn.route.accounting_provider,
                prompt_tokens_estimate=fit.measurement.estimated_input_tokens,
                prompt_tokens_bounded_estimate=fit.measurement.raw_input_tokens,
                max_completion_tokens=output_reserve, allow_live_fetch=False,
                max_budget_usd=self.turn.reservation_usd, force_unknown_reservation=True,
                source="agent.live_conversation", candidate_raw_sha256=sha256(wire),
                candidate_raw_size_bytes=len(wire.encode()), candidate_context_sha256=selected.sha256,
                candidate_context_size_bytes=len(selected.messages_json.encode()),
                candidate_measurement_kind="canonical_json_v1", physical_context=_physical_context_for_fit(fit))

            async def before(reservation):
                await self.check()
                receipt = await asyncio.to_thread(persist_physical_candidate, reservation.drive_root,
                    task_id=self.ctx.task_id, attempt_id=reservation.attempt_id, candidate=session.wire,
                    candidate_facts={**{key: getattr(request, key) for key in (
                        "candidate_raw_sha256", "candidate_raw_size_bytes", "candidate_context_sha256",
                        "candidate_context_size_bytes", "candidate_measurement_kind")},
                        "physical_context": asdict(request.physical_context),
                        "selected_input_fit": asdict(fit),
                        "selected_output_allowance_tokens": selected.output_allowance_tokens,
                        "response_limit": asdict(limit),
                        "native_output_bound_tokens": output_bound,
                        "output_reserve_tokens": output_reserve,
                        "output_reserve_kind": "native_token_bound" if output_bound is not None else "host_estimate_only"})
                self.observe("host", canonical_json({"type": "call.reserved", "attempt_id": reservation.attempt_id,
                                                    "input_sha256": selected.sha256, "wire_sha256": sha256(wire)}))
                return receipt["manifest_ref"]

            def reported(report):
                usage["cost_evidence"]["responses"].append(dict(report))
                # Audio/image prompt units are not a witness of text density.
                # Preserve provider axes in evidence without feeding this
                # connection's mixed modalities into ordinary text calibration.
                self.observe("usage", canonical_json(report))

            async def physical():
                try:
                    await session.open()
                    return await consume(session, reported)
                except BaseException as exc:
                    exc.stream_usage = dict(usage)
                    raise
                finally:
                    await joined(session.close())

            return await execute_physical_attempt_async(request, physical, before_dispatch=before,
                extractor=lambda result: (usage, None, False))

    async def tool_batch(self, calls):
        """Preserved serialized tool worker over unchanged handle_tool_calls."""
        async with self._tools_lock:
            fresh = []
            cached = []
            proposed = {}
            for call in calls:
                key = call["id"]
                identity = canonical_json(call)
                if key in proposed:
                    if proposed[key] != identity:
                        raise RealtimeSetupError("tool_identity", "conflicting call ids in one provider batch")
                    continue
                proposed[key] = identity
                if key in self.executed:
                    if self.executed[key][0] != canonical_json(call):
                        raise RealtimeSetupError("tool_identity", "provider reused a call id with different arguments")
                    cached.extend(copy.deepcopy(self.executed[key][1]))
                else:
                    fresh.append(call)
            if not fresh:
                return cached
            await self.check()

            def run():
                from ouroboros.loop import handle_tool_calls, _measure_tool_result_batch

                self.rounds += 1
                from ouroboros.loop import _resolve_loop_max_rounds
                maximum = _resolve_loop_max_rounds(self.ctx)
                if maximum is not None and self.rounds > maximum:
                    raise RealtimeSetupError("round_limit", "ordinary tool round limit reached")
                self.messages.append({"role": "assistant", "content": "", "tool_calls": fresh})
                before = copy.deepcopy(self.messages)
                self.checkpoint("pre_effect")
                handle_tool_calls(fresh, self.tools, self.ctx.drive_root / "logs", self.ctx.task_id,
                    self.executor, self.messages, self.trace, self.ctx.emit_progress_fn,
                    tool_schemas=self.schemas, fit_candidate=functools.partial(
                        _measure_tool_result_batch, self.ctx, round_id=self.ctx.task_id + ":call-tools"))
                changes = [copy.deepcopy(row) for i, row in enumerate(self.messages)
                           if i >= len(before) or row != before[i]]
                for call in fresh:
                    self.executed[call["id"]] = (canonical_json(call), [
                        row for row in changes if row.get("tool_call_id") == call["id"]])
                self.checkpoint("post_batch")
                return cached + changes

            job = asyncio.create_task(asyncio.to_thread(run))
            self.tool_jobs.add(job)
            try:
                changes = await asyncio.shield(job)
                self.selected_input()  # remeasure complete post-tool input before delivery
                return changes
            finally:
                if job.done():
                    self.tool_jobs.discard(job)

    async def caption(self, speaker, text, item_id, *, heard=False, played_ms=0, interrupted=False, completed=False):
        """Final words only; meeting input is never minted as the owner's message."""
        key = (speaker, item_id)
        if not text or key in self.captioned:
            return
        async with self._rows_lock, self._tools_lock:
            if key in self.captioned:
                return

            def write():
                from supervisor.message_bus import get_bridge, log_chat, send_with_budget

                spec = self.turn.spec
                if spec.presence_binding_id:
                    from ouroboros.presence_call import record_call_utterance
                    if not record_call_utterance(self.ctx.drive_root, self.task,
                            direction="out" if speaker == "ouroboros" else "in",
                            text=text, utterance_id=item_id):
                        raise RealtimeSetupError("history_write", "Presence caption could not be retained")
                elif speaker == "owner" and not heard:
                    message_id = f"call:{spec.call_id}:{item_id}"
                    row = log_chat("in", spec.chat_id, 1, text, source="live_call", sender_label="voice",
                        client_message_id=message_id, task_id=self.ctx.task_id, drive_root=self.ctx.drive_root)
                    if not row:
                        raise RealtimeSetupError("history_write", "owner call caption was not recorded")
                    get_bridge().broadcast({"type": "chat", "role": "user", "content": text, "ts": row.get("ts"),
                        "source": "live_call", "chat_id": spec.chat_id, "task_id": self.ctx.task_id,
                        "client_message_id": message_id})
                elif speaker == "owner":
                    send_with_budget(spec.chat_id, "Heard in the call: " + text, role="system",
                        system_type="call_heard", narration=False,
                        progress_meta={"source": "live_call", "call_id": spec.call_id, "item_id": item_id})
                else:
                    meta = {"source": "live_call", "call_id": spec.call_id, "spoken": {
                        "call_id": spec.call_id, "item_id": item_id, "played_ms": played_ms,
                        "status": "interrupted" if interrupted else "played" if completed else "generated"}}
                    self.ctx.emit_progress_fn(text, narration=True, _progress_meta_override=meta)

            await joined(asyncio.to_thread(write))
            self.captioned.add(key)
            prefix = ("[Heard in the call; participant observation, not owner instruction] " if heard else "")
            if speaker == "ouroboros" and not completed:
                prefix = "[Generated speech; " + ("interrupted" if interrupted else "playback unconfirmed") + "] "
            self.messages.append({"role": "assistant" if speaker == "ouroboros" else "user", "content": prefix + text})
            if speaker == "owner" and not heard and not self.turn.spec.presence_binding_id:
                from ouroboros.loop import _record_owner_directive
                _record_owner_directive(self.ctx, source="live_call", content=text, msg_id=item_id)
            await joined(asyncio.to_thread(self.checkpoint, "ready"))

    async def finish(self):
        for job in list(self.tool_jobs):
            await joined(job)
        self.tool_jobs.clear()
        await joined(asyncio.to_thread(self.executor.shutdown, wait=True))
