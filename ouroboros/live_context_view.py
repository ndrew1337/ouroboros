"""Live boundaries around the ordinary observed/authored context-view owners."""
from __future__ import annotations

import copy


def pending_context_view(host):
    return (getattr(host.ctx, '_pending_compaction', None) is not None
            or getattr(host.ctx, '_pending_tool_schema_names', None) is not None)


def record_session_view(host, evidence, manifest_ref):
    """Only an opened connection exposes a new canonical prefix to the mind."""
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
            host._view_session_factory, host._view_session_options)
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
