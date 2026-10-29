"""Live boundaries around the ordinary observed/authored context-view owners."""
from __future__ import annotations

import copy


def pending_context_view(host):
    return (getattr(host.ctx, '_pending_compaction', None) is not None
            or getattr(host.ctx, '_pending_tool_schema_names', None) is not None)


def record_session_view(host, evidence, manifest_ref):
    """An opened connection supplies inspect; a usable response establishes exposure."""
    from ouroboros.tools.compact_context import record_context_view

    record_context_view(host.ctx, host.messages, host.schemas, fit_measurement=evidence['fit']['measurement'])
    host.ctx._last_context_observation.update(
        work_room=copy.deepcopy(evidence['work_room']), physical_source_ref=manifest_ref)
    if evidence['work_room'].get('reason') == 'start_room_shortfall' and host.ctx.emit_progress_fn:
        # The progress notice reaches the owner even when the physical candidate
        # cannot accommodate another in-model notice without losing held content.
        host.ctx.emit_progress_fn(evidence['work_room']['detail'])
    receipt = getattr(host.ctx, '_context_view_receipt', None)
    if receipt and receipt.get('status') == 'applied':
        receipt['sent_fit'] = {**evidence['fit']['measurement'], 'wire_sha256': evidence['wire_sha256']}
    host._session_opened = True


def record_live_exposure(view, response_id):
    """Usable answers expose only rows delivered by this adopted connection.

    Native adapters add rows after their delivery boundary, never while commands
    are queued behind speech. A speculative or retired connection cannot teach
    the current observed view. Setup alone still supplies inspect, not exposure.
    """
    from ouroboros.context_compaction import exposed_context_units
    from ouroboros.tools.compact_context import record_context_view
    owner = getattr(view, '_connection_owner', view)
    session = getattr(view, '_context_session', None)
    if session is None or getattr(owner, '_connection_view', view) is not view:
        return
    held = getattr(session, 'response_context_rows', {}).pop(response_id, None)
    if held is None:
        return  # older plugins supply no delivery evidence; unknown stays unknown
    # Snapshot on the boundary; hashing belongs to a worker, never the media loop.
    import asyncio
    previous = copy.deepcopy(getattr(owner.ctx, '_last_context_observation', {}) or {})
    context, rows, schemas = copy.copy(owner.ctx), copy.deepcopy(owner.messages), copy.deepcopy(owner.schemas)
    def calculate():
        record_context_view(context, rows, schemas, fit_measurement=previous.get('fit_measurement'))
        context._last_context_observation.update(
            exposed_units=list(exposed_context_units(rows, held)), exposure_basis='live_delivered_rows',
            **{key: previous[key] for key in ('work_room', 'physical_source_ref') if key in previous})
        return context._last_context_observation
    def publish(observation):
        if getattr(owner, '_connection_view', view) is view:
            owner.ctx._last_context_observation = observation
            view.ctx._last_context_observation = copy.deepcopy(observation)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        publish(calculate())  # synchronous callers have no media loop
        return
    previous_job = getattr(owner, '_exposure_job', None)
    async def record():
        if previous_job is not None:
            await previous_job
        publish(await asyncio.to_thread(calculate))
    owner._exposure_job = asyncio.create_task(record())


async def drain_exposure(host):
    from ouroboros.realtime_session import joined
    job = getattr(host, '_exposure_job', None)
    if job is not None:
        await joined(job)


def serving_delivery_fits(host, rows, schemas):
    """Price the serving connection's held input plus undelivered canonical growth.

    Its delivered catalog is authoritative after a failed declaration refit. New
    offer pointers not in canonical history are counted too; nothing is removed.
    """
    import math
    from collections import Counter
    from ouroboros.context_fit import estimate_context_prompt_tokens
    from ouroboros.llm_messages import project_wire_messages
    from ouroboros.contracts.realtime import canonical_json
    facts = host.model.facts
    window, reserve = facts.conversation_window_tokens, facts.working_reserve_tokens or 0
    if host._renewal_window is not None and host._renewal_facts == facts:
        window, reserve = host._renewal_window.total_equivalent, host._renewal_window.reserve
    if not window:
        return True  # no measured window has been declared
    density = getattr(host._physical_context, 'measurement_density', 1.0) if facts.basis == 'provider' else 1.0
    held = host._held_input or math.ceil(host._selected_raw_estimate * density)
    current = list(host.messages)
    counts = Counter(canonical_json(row) for row in project_wire_messages(current))
    for row, wire in zip(rows, project_wire_messages(rows)):
        if row.get('_canonical'):
            continue  # producer already recorded this offer, with its host-attribution wrapper
        key = canonical_json(wire)
        if counts[key]:
            counts[key] -= 1
        else:
            current.append(row)
    if host._session_instructions:
        current.append({'role': 'system', 'content': host._session_instructions})
    pending = math.ceil(max(0, estimate_context_prompt_tokens(current, schemas) - host._history_at_receipt) * density)
    # A canonical fold cannot free the serving socket: price the delivery itself too.
    pending = max(pending, math.ceil(estimate_context_prompt_tokens(rows) * density))
    return held + pending + host._growth + reserve <= window


def apply_context_view(host):
    """Called under the tool lock at the driver's quiet boundary; no new effect is dispatched."""
    from ouroboros.context_compaction import context_reclaim_transcript_sha256
    from ouroboros.loop_round_limits import _CompactionRoundContext, _run_authored_context_view
    from ouroboros.realtime_setup import measure_session_wire
    from ouroboros.tools.owner_delivery import publish_pending_owner_dialogue
    from ouroboros.contracts.realtime import canonical_json

    publish_pending_owner_dialogue(host.ctx, host.messages)
    pending = getattr(host.ctx, '_pending_compaction', None)
    selected_names = getattr(host.ctx, '_pending_tool_schema_names', None)
    if not pending_context_view(host):
        return False

    def measure(candidate, schemas):
        from ouroboros.tools.tool_discovery import update_resident_notice
        candidate = copy.deepcopy(candidate)
        if any(row.get('_host_context_kind') == 'tool_manifest' for row in candidate):
            update_resident_notice(host.tools, schemas, candidate)
        _, _, evidence, _ = measure_session_wire(host, host.plan, candidate, schemas,
            host._view_session_factory, host._view_session_options, fresh=False, _room_fact=False)
        # Ordinary Main's authored-view policy: predictions inform dispatch;
        # they do not veto the mind's view. The next open still checks capacity.
        return {'accepted': True, 'strict_bound_proven': False,
                **evidence['fit']['measurement'], 'wire_sha256': evidence['wire_sha256']}

    before = measure(host.messages, host.schemas)
    old = context_reclaim_transcript_sha256(host.messages), copy.deepcopy(host.schemas)
    context = _CompactionRoundContext(host.tools, host.ctx.drive_root, host.ctx.drive_root / 'logs',
        host.ctx.task_id, host.rounds, host.event_queue, host.ctx.emit_progress_fn or (lambda text: None),
        host.schemas, measure, extra_tool_schemas=tuple({'type': 'function', 'function': {
            'name': tool.name, 'description': tool.description, 'parameters': dict(tool.parameters)}}
            for tool in host.call_tools.values()))
    candidate = _run_authored_context_view(host.messages, context, pending, selected_names)
    host.plan = host.ctx.context_fit_plan
    host.messages[:] = candidate
    host._context_changed()
    receipt = host.ctx._context_view_receipt
    receipt['fit_before'] = before
    host.observe('host', canonical_json({'type': 'call.context_view', 'receipt': receipt}))
    host.checkpoint('ready')
    return old != (context_reclaim_transcript_sha256(host.messages), host.schemas)
