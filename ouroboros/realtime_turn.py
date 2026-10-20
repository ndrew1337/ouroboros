"""An ordinary admitted room turn using a realtime transport.

Preparation, tools, controls and money keep their ordinary owners. This first
consumer previews a whole text input, waits for explicit funding of those bytes,
and owns one setup/probe through actual teardown. It is not an audio/tool loop.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import threading
import time
from dataclasses import asdict, replace

from ouroboros.contracts.realtime import USAGE_AXES, RealtimeProbe, RealtimeSetupError, canonical_json


def _positive_number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise RealtimeSetupError(name, "must be a finite positive number")
    return float(value)


def _reported_usage(report):
    """The provider's report in host axes; absent or malformed axes stay unknown."""
    report = report if isinstance(report, dict) else {}
    return {axis: report[axis] for axis in USAGE_AXES if type(report.get(axis)) is int and report[axis] >= 0}


def _source_bytes(value):
    # ReferenceBook/MarkdownSource retains its original UTF-8 bytes as well as
    # parsed spans. Preserve them reversibly in the full source capture.
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    raise TypeError(f"unsupported captured source value: {type(value).__name__}")


def recovery_refusal(task, turn):
    """A persisted transport intent cannot recreate its process-local authority."""
    request = (task.get("metadata") or {}).get("execution_transport")
    if request is None:
        return ""
    task["_skip_post_task_synthesis"] = True
    return ("" if isinstance(turn, RealtimeTurn) and turn.matches_live_request(request)
            else "realtime_restart_required")


class RealtimeTurn:
    """Process-local transport choice, minted at authenticated owner ingress.

    No serializable metadata can grant this capability. The active direct actor
    owns this object; a different turn, peer, or recovered task cannot execute it.
    """

    def __init__(self, request, *, publish):
        from ouroboros.realtime_setup import loaded_provider

        allowed = {"kind", "provider", "model", "timeout_s", "probe"}
        if not isinstance(request, dict) or set(request) - allowed or request.get("kind") != "realtime":
            raise RealtimeSetupError("transport", "unsupported realtime transport fields")
        model = request.get("model")
        if not isinstance(model, str) or not model or any(c not in
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._" for c in model):
            raise RealtimeSetupError("model", "an explicit native provider model id is required")
        # Selected by registry key: the loaded registration declares its route,
        # and every later provider call is bound to this generation.
        self.provider = str(request.get("provider") or "")
        spec = loaded_provider(self.provider)
        self.route, self._generation = spec["route"], spec["extension_generation"]
        self._content_hash = spec["content_hash"]
        self.native_model = model
        self.model = self.route.model_prefix + model
        self.timeout_s = _positive_number(request.get("timeout_s"), "deadline")
        self.deadline = time.monotonic() + self.timeout_s
        raw_probe = request.get("probe")
        if not isinstance(raw_probe, dict) or set(raw_probe) != {"text", "max_output_tokens"}:
            raise RealtimeSetupError("probe", "supply the explicit bounded text probe to preview")
        self.probe = RealtimeProbe(**raw_probe)
        self.request = json.loads(canonical_json(request))
        self.publish = publish
        self._lock = threading.Lock()
        self._wire_sha256 = ""
        self._funding = None
        self._closed = False

    def matches_live_request(self, request):
        with self._lock:
            return not self._closed and request == self.request

    def authorize(self, wire_sha256, reservation_usd):
        funds = _positive_number(reservation_usd, "funding")
        with self._lock:
            if self._closed or time.monotonic() >= self.deadline:
                raise RealtimeSetupError("ended", "this admitted turn has ended or expired")
            if not self._wire_sha256 or wire_sha256 != self._wire_sha256:
                raise RealtimeSetupError("wire_changed", "funding must name this actor's exact preview")
            if self._funding is not None:
                raise RealtimeSetupError("already_authorized", "one physical attempt only")
            self._funding = funds

    def resolve_route(self, task, *, allow_fetch):
        """Ordinary fitting against this transport's exact cached route evidence.

        Neither a Main-role window nor a chat-completions endpoint describes a
        realtime socket. Unknown evidence stays unknown; no model/window table.
        """
        from ouroboros.capability_evidence import probe
        from ouroboros.config import DATA_DIR
        from ouroboros.response_limits import resolve_response_limit

        route = dict(provider=self.route.accounting_provider, model=self.model,
                     base_url=self.route.endpoint, use_local=False,
                     model_role="main", options={})
        args = {key: route[key] for key in ("provider", "model", "base_url")}
        evidence = probe(DATA_DIR, **args, allow_fetch=False)
        limit = resolve_response_limit(DATA_DIR, **args, allow_fetch=False)
        return route, replace(evidence, response_limit=asdict(limit))

    def run(self, ctx, messages, tools, *, task, initial_effort):
        """Synchronous actor entry; asyncio.run joins its owned async work."""
        from ouroboros.artifacts import store_actor_source_bytes, task_artifact_dir_path
        from ouroboros.context_fit import measure_main_fit
        from ouroboros.realtime_setup import input_from_context_plan
        from ouroboros.tool_policy import initial_tool_schemas
        from ouroboros.usage_accounting import BudgetExceeded

        identity = dict(task_id=ctx.task_id, chat_id=ctx.current_chat_id,
                        client_message_id=(task.get("metadata") or {}).get("client_message_id", ""))
        wire_path = task_artifact_dir_path(ctx.drive_root, ctx.task_id, create=True) / "realtime-wire.jsonl"
        reported = {}  # the provider's latest usage report, host axes only
        usage = {"terminal_origin": "host_notice", "execution_status": "infra_failed"}
        trace = {"reasoning_notes": [], "tool_calls": []}

        def observe(direction, raw):
            # Fail closed if raw evidence cannot be retained before interpretation.
            with wire_path.open("a", encoding="utf-8") as stream:
                stream.write(canonical_json({"direction": direction, "raw": raw}) + "\n")
                stream.flush()
            if direction == "usage":
                reported.clear()
                reported.update(_reported_usage(json.loads(raw)))

        try:
            if not ctx.is_direct_chat or not task.get("origin_message_ref"):
                raise RealtimeSetupError("owner_ingress", "a canonical admitted owner-room turn is required")
            plan = ctx.context_fit_plan
            if plan.model != self.model or plan.provider != self.route.accounting_provider:
                raise RealtimeSetupError("route", "prepared input must be fitted for the actual realtime route")
            mode = plan.initial_mode
            schemas = initial_tool_schemas(tools, context_mode=mode)
            # The explicit probe is an additional user item on this one wire.
            # Include its cost in the ordinary fit without changing the prepared
            # cognition messages or introducing another allowance policy.
            measured_messages = [*messages, {"role": "user", "content": self.probe.text}]
            fit = measure_main_fit(
                plan, measured_messages, schemas,
                profile=("owner_" if mode == plan.preferred_mode else "task_local_") + mode,
                rendered_mode=mode, round_id=ctx.task_id + ":realtime", reasoning_effort=initial_effort)
            # The diagnostic response is bounded separately from ordinary Nano's
            # reply floor. Measure the same complete input with its actual bound.
            physical_fit = measure_main_fit(
                replace(plan, output_reserve_tokens=self.probe.max_output_tokens), measured_messages, schemas,
                profile=fit.measurement.profile, rendered_mode=mode,
                round_id=fit.measurement.round_id, reasoning_effort=initial_effort)
            selected = input_from_context_plan(
                plan, model=self.native_model, selected_tools=schemas, messages=messages,
                output_allowance_tokens=fit.measurement.reply_allowance_tokens)

            def save_preview(preview):
                payload = dict(
                    version=1, kind="realtime_prepared_input", **identity,
                    origin_message_ref=task["origin_message_ref"], selected=asdict(selected),
                    context_fit_plan=asdict(plan), fit=asdict(fit), physical_fit=asdict(physical_fit),
                    prepared_messages=messages, probe=asdict(self.probe), wire=json.loads(preview.wire_json),
                    input_sha256=selected.sha256, wire_sha256=preview.wire_sha256,
                    provider_generation=self._generation, provider_content_hash=self._content_hash)
                ref = store_actor_source_bytes(
                    ctx.drive_root, ctx.task_id, category="context_checkpoints",
                    source_id="realtime-prepared", data=json.dumps(
                        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                        allow_nan=False, default=_source_bytes).encode(), extension="json")
                with self._lock:
                    self._wire_sha256 = preview.wire_sha256
                self.publish(dict(type="realtime_prepared", **identity, source_ref=ref,
                                  input_sha256=selected.sha256, wire_sha256=preview.wire_sha256,
                                  mode=mode, output_allowance_tokens=selected.output_allowance_tokens,
                                  provider_generation=self._generation))
                return ref

            result = asyncio.run(self._run_owned(ctx, selected, physical_fit, observe, save_preview, reported))
            usage.update(reported, execution_status="ok", reason_code=result.state)
            text = "Realtime probe completed. Raw response and prepared input are retained in this task's artifacts."
        except BaseException as exc:
            if isinstance(exc, BudgetExceeded):
                usage.update(reason_code="budget_exhausted", execution_status="failed")
                raise  # The ordinary agent owns its resource-limit result.
            if not isinstance(exc, (Exception, asyncio.CancelledError)):
                raise
            code = getattr(exc, "code", "") or ("cancelled" if isinstance(exc, asyncio.CancelledError)
                                                else "realtime_failed")
            usage.update(reported, reason_code=code,
                         execution_status="cancelled" if code in {"cancelled", "finalize_requested"} else "infra_failed")
            text = f"Realtime turn ended: {exc or code}."
        finally:
            # Both provider and accounting teardown have finished before this fact
            # or the ordinary direct-activity unregister can be published.
            with self._lock:
                self._closed = True
            self.publish(dict(type="realtime_finished", **identity,
                              reason=usage.get("reason_code", "realtime_failed"),
                              execution_status=usage["execution_status"]))
        return text, usage, trace

    async def _run_owned(self, ctx, selected, fit, observe, save_preview, reported):
        from ouroboros.observability import persist_physical_candidate
        from ouroboros.realtime_setup import run_realtime_setup
        from ouroboros.usage_accounting import (
            AttemptRequest, PhysicalAttemptPreconditionFailed,
            execute_physical_attempt_async, last_physical_attempt_capture,
        )
        from ouroboros.loop_model_call import _physical_context_for_fit

        waiter = ctx.model_wait_context
        if waiter is None:
            raise RealtimeSetupError("host_control", "the admitted actor must own a model-wait scope")

        async def check():
            reason = await asyncio.to_thread(waiter.control_reason, strict_current=True)
            if reason:
                raise RealtimeSetupError(reason, "ordinary task control ended the realtime turn")
            if time.monotonic() >= self.deadline:
                raise RealtimeSetupError("deadline", "realtime turn deadline reached")

        async def operation():
            await check()
            preview = await run_realtime_setup(
                self.provider, selected, timeout_s=max(.001, self.deadline - time.monotonic()),
                observe=observe, probe=self.probe, expected_generation=self._generation)
            source_ref = save_preview(preview)
            while True:
                await check()
                with self._lock:
                    funds = self._funding
                if funds is not None:
                    break
                await asyncio.sleep(.05)
            # Revalidate the loaded generation and exact bytes before reserving.
            current = await run_realtime_setup(
                self.provider, selected, timeout_s=max(.001, self.deadline - time.monotonic()),
                observe=observe, probe=self.probe, expected_generation=self._generation)
            if current.wire_sha256 != preview.wire_sha256:
                raise RealtimeSetupError("wire_changed", "provider preview changed before execution")
            request = AttemptRequest(
                model=self.model, provider=self.route.accounting_provider,
                prompt_tokens_estimate=fit.measurement.estimated_input_tokens,
                prompt_tokens_bounded_estimate=fit.measurement.raw_input_tokens,
                max_completion_tokens=self.probe.max_output_tokens, max_budget_usd=funds,
                source="agent.realtime", allow_live_fetch=False,
                candidate_raw_sha256=preview.wire_sha256,
                candidate_raw_size_bytes=len(preview.wire_json.encode()),
                candidate_context_sha256=selected.sha256,
                candidate_context_size_bytes=len(selected.messages_json.encode()),
                candidate_measurement_kind="canonical_json_v1",
                physical_context=_physical_context_for_fit(fit))
            reported.clear()

            async def send():
                try:
                    return await run_realtime_setup(
                        self.provider, selected, timeout_s=max(.001, self.deadline - time.monotonic()),
                        observe=observe, execute=True, expected_wire_sha256=preview.wire_sha256,
                        expected_generation=self._generation, probe=self.probe)
                except BaseException as exc:
                    # The ordinary physical-attempt owner settles reported usage
                    # even on a failed response or a Stop racing with a late frame.
                    if reported:
                        exc.stream_usage = dict(reported)
                    raise

            async def before_dispatch(reservation):
                await check()
                # Preview is free even when this physical request cannot fit.
                # The actual bounded probe, not ordinary Nano's floor, owns admission.
                if fit.predicted_capacity_miss:
                    failure = PhysicalAttemptPreconditionFailed(
                        "context_capacity: complete input plus bounded probe exceeds the known window",
                        attempt_id=reservation.attempt_id)
                    failure.code = "context_capacity"
                    raise failure
                observe("host", canonical_json({
                    "type": "realtime.funded_attempt", "attempt_id": reservation.attempt_id,
                    "reservation_usd": funds, "wire_sha256": preview.wire_sha256,
                    "prepared_source_ref": source_ref}))
                # The ordinary model_send seal owner records this exact send copy.
                persisted = await asyncio.to_thread(
                    persist_physical_candidate, reservation.drive_root, task_id=ctx.task_id,
                    attempt_id=reservation.attempt_id, candidate=json.loads(preview.wire_json),
                    candidate_facts={**{key: getattr(request, key) for key in (
                        "candidate_raw_sha256", "candidate_raw_size_bytes", "candidate_context_sha256",
                        "candidate_context_size_bytes", "candidate_measurement_kind")},
                        "physical_context": (dict(vars(request.physical_context))
                                             if request.physical_context is not None else None)})
                return persisted["manifest_ref"]

            try:
                return await execute_physical_attempt_async(
                    request, send, before_dispatch=before_dispatch,
                    extractor=lambda result: (dict(reported), None, False))
            finally:
                capture = last_physical_attempt_capture()
                if capture is not None:
                    observe("host", canonical_json({"type": "realtime.attempt_finished", **asdict(capture)}))

        async def controls():
            while True:
                await check()
                await asyncio.sleep(.05)

        work, control = asyncio.create_task(operation()), asyncio.create_task(controls())
        try:
            done, _ = await asyncio.wait((work, control), return_when=asyncio.FIRST_COMPLETED)
            if control in done:
                await control
            return await work
        finally:
            # Request cancellation once, then join despite repeated cancellation.
            # The provider lease and physical-attempt owner retain their teardown.
            for child in (work, control):
                if not child.done():
                    child.cancel()
            for child in (work, control):
                while not child.done():
                    try:
                        await asyncio.shield(child)
                    except BaseException:
                        if child.done():
                            break
                if not child.cancelled():
                    child.exception()
