"""The route-owned work order of one acceptance panel's RETRIEVING rows.

A packet row reviews what the host assembled; a retrieving row reads the task's
own sources. Both receive the same task, criteria and output contract — what
differs is what each delivery can actually use, which is this module's whole
reason to change. Extracted from ``loop_acceptance_review`` (size paydown): the
panel's orchestration and this delivery contract have separate reasons to change
and separate readers.
"""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Any, Dict, List, Optional


_RETRIEVING_ACCESS_DISCLOSURE = (
    "Access outside the task workspace is not guaranteed on this delivery: a refused or "
    "failed read is absence of evidence, not absence of the artifact — report it as a gap "
    "you could not verify instead of inferring the artifact does not exist."
)



def _retrieving_packet_projection(evidence: Dict[str, Any]) -> Dict[str, Any]:
    from ouroboros.review_dispatch import retrieving_acceptance_packet

    return retrieving_acceptance_packet(evidence)


def acceptance_retrieving_work_order(
    request: Any, slots: List[Any], *, session_root: str, data_root: pathlib.Path,
) -> None:
    """Attach the route-owned work order of ONE acceptance panel's retrieving
    rows (owner R1/R4/R5/R15, 2026-09-01) to ``request`` in place.

    Every retrieving row receives the same task, criteria and output contract
    as the packet rows — rendered by the same `_render_prompt_parts` — plus
    absolute retrieval pointers. A SESSION row gets the FULL packet (its run is
    unobserved by the host, so the packet is its only attested view) and the
    access disclosure; a NATIVE row gets the packet without its freely
    degradable tail and the real data root (R5), because its episode reads
    task results and artifacts itself. The FULL packet stays on
    ``request.evidence``: evidence_refs resolve against it, never against a
    rendered projection."""
    from ouroboros.artifacts import task_artifact_dir_path
    from ouroboros.outcome_receipt_store import verification_receipts_path
    from ouroboros.review_execution import ReviewRouteKind, _render_prompt_parts, review_output_contract

    request.session_root = session_root
    request.policy["output_contract"] = review_output_contract(request)
    request.policy["native_data_root"] = str(data_root)
    task_id = str(request.task_id or "")
    root = pathlib.Path(data_root)
    try:
        artifacts_dir, receipts = task_artifact_dir_path(root, task_id), verification_receipts_path(root, task_id)
    except Exception:  # an unusual task id: name the canonical layout instead of refusing the work order
        artifacts_dir = root / "task_results" / "artifacts" / task_id
        receipts = artifacts_dir / "verification_receipts.jsonl"
    pointers = "\n".join((
        "RETRIEVAL POINTERS (absolute paths; the packet below is the host's attested projection of these sources):",
        f"- task workspace — the active tree the task worked in (your root): {session_root}",
        f"- task result record (contract, status, children): {root / 'task_results' / (task_id + '.json')}",
        f"- task artifacts named by the packet's `artifacts` manifest: {artifacts_dir}/",
        f"- host-attested verification receipts: {receipts}",
        f"- tool trajectory log (rows with task_id={task_id}): {root / 'logs' / 'tools.jsonl'}",
    ))
    native_packet: Optional[Dict[str, Any]] = None
    for slot in slots:
        if getattr(slot, "route", None) is ReviewRouteKind.AGENT_SESSION:
            preamble = (
                "You review as a read-only agent session in the task workspace. The host's FULL evidence "
                "packet follows; verify its claims against the sources at the pointers with your own tools. "
                + _RETRIEVING_ACCESS_DISCLOSURE
            )
            packet = request.evidence
        else:
            preamble = (
                "You review as a bounded read-only native inspection episode; the host data root at the "
                "pointers is readable. The evidence packet follows WITHOUT its tool-trajectory rows and "
                "artifact previews — read those sources yourself at the pointers."
            )
            if native_packet is None:
                native_packet = _retrieving_packet_projection(request.evidence)
            packet = native_packet
        _stable, task_stable, dynamic = _render_prompt_parts(dataclasses.replace(request, evidence=packet), slot)
        slot_line = f"Slot: {slot.slot_id}"
        dynamic = dynamic.rstrip()  # the renderer's tail may grow a newline; the executor labels the slot itself
        if dynamic.endswith(slot_line):
            dynamic = dynamic[: -len(slot_line)].rstrip()
        request.slot_session_tasks[slot.slot_id] = "\n\n".join((
            preamble,
            pointers,
            "Every evidence_ref must be an EXACT member of the packet's host-attested exhibit vocabulary; "
            "the FULL packet is the host's resolution authority whatever you read at the pointers.",
            task_stable.rstrip() + "\n\n" + dynamic,
        ))
