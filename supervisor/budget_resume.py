"""Exact-continuation budget Resume grants and their revocation (#1196).

The owner's explicit Resume of a task paused MID-RUN mints ONE single-use grant
bound to one pause id and one resume generation; the grant rides the queue row
as ``_budget_pause_resume`` until a worker consumes it, and returns to the
pause — never to a replay or a terminal — when money vanishes, a restart
intervenes, or its revocation cannot be written (then the row is HELD, typed,
with its pause marker retained). Split out of ``supervisor/queue_transitions.py``
at that module's band ceiling: the grant lifecycle is one owner with its own
reason to change (owner Q7/Q9/Q10 semantics), and ``queue_transitions`` keeps
the general resume seam (``resume_budget_paused_task``) that calls into it.
Every call here runs with the queue lock held by that seam or by restore.
"""

from __future__ import annotations

import logging
import pathlib
import time
import uuid
from typing import Any, Dict, List, Optional

from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)


def grant_exact_budget_resume(task: Dict[str, Any], pause: Dict[str, Any],
                              *, selected_by: str = "",
                              external: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Mint ONE pause/generation-bound grant under the queue lock.

    Refuse unknown/stale money, exhausted wallets, Stop, deadline, finite
    lifetime, an unreadable checkpoint or unsettled custody; a stale queue
    locator is refreshed from the durable pause, never trusted. ``paused_duration_sec``
    carries the paused interval without moving ``started_at`` or resetting the
    quota clock. ``external`` is THIS grant's fresh custody observation (else read
    here); a model ``selected_by`` needs its root's live owner-derived grant (Q9).
    """
    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.budget_pause import (
        EXTERNAL_STOP_CONFIRMED, LIVE_PAUSE_STATES, STATE_PAUSED, STATE_RESUME_GRANTED,
        exact_pause_marker, observe_task_runs, set_budget_pause,
    )
    from ouroboros.cancel_intents import has_active_intent
    from ouroboros.config import get_task_abs_ceiling_sec
    from ouroboros.deadline_utils import parse_deadline_ts, utc_now
    from ouroboros.model_wait import execution_elapsed_seconds
    from ouroboros.task_results import _TRULY_TERMINAL_STATUSES, load_task_result
    from supervisor.events_budget import (
        BUDGET_HOLD_KEY, HOLD_REVOCATION_UNWRITTEN, HOLD_MALFORMED_RESUME_IDENTITY,
        hold_budget_row, live_root_resume_grant, hold_root_resume_descendants,
    )
    from supervisor import queue as q
    from supervisor.state import budget_remaining

    task_id = str(task.get("id") or "")
    checkpoint = pause.get("checkpoint") if isinstance(pause.get("checkpoint"), dict) else {}
    pause_id = str(checkpoint.get("pause_id") or "")
    if not pause_id.strip():
        return {"ok": False, "error": "malformed_pause_identity"}
    result_root = pathlib.Path(task.get("budget_drive_root") or q.DRIVE_ROOT)
    if any((pathlib.Path(q.DRIVE_ROOT) / "state" / name).exists()
           for name in ("owner_restart_no_resume.flag", "panic_stop.flag")):
        return {"ok": False, "error": "restart_no_resume", "action": "wait_or_cancel"}
    try:
        result_row = load_task_result(result_root, task_id, strict=True) or {}
    except Exception:
        return {"ok": False, "error": "pause_record_unreadable", "action": "cancel_or_new_run"}
    row = result_row.get("budget_pause") if isinstance(result_row.get("budget_pause"), dict) else {}
    if result_row.get("status") in _TRULY_TERMINAL_STATUSES:
        return {"ok": False, "error": "task_terminal"}
    if (row and row.get("state") in LIVE_PAUSE_STATES and row.get("source_ref")
            and str(row.get("pause_id") or "") and str(row.get("pause_id") or "") != pause_id):
        # Refresh the stale locator from the durable pause, retaining the queue's fence id.
        refreshed = exact_pause_marker(row, default_root=str(pause.get("root_task_id")
                                                             or task.get("root_task_id") or task_id))
        if pause.get("fence_id"):
            refreshed["fence_id"] = pause["fence_id"]
        task["_budget_pause"] = refreshed
        q.append_jsonl(q.DRIVE_ROOT / "logs" / "events.jsonl",
                       {"ts": utc_now_iso(), "type": "budget_pause_marker_refreshed", "task_id": task_id,
                        "stale_pause_id": pause_id, "pause_id": str(row.get("pause_id") or ""),
                        "pause_generation": int(row.get("pause_generation") or 0)})
        pause, checkpoint = refreshed, refreshed["checkpoint"]
        pause_id = str(row.get("pause_id") or "")
    if (not row or row.get("pause_id") != pause_id or row.get("state") not in LIVE_PAUSE_STATES
            or not row.get("source_ref")):
        return {"ok": False, "error": "pause_record_missing", "action": "cancel_or_new_run"}
    if int(row.get("task_attempt") or 0) != int(task.get("_attempt") or 1):
        # Another attempt than the checkpoint's: the loop would refuse the grant on arrival.
        return {"ok": False, "error": "pause_attempt_mismatch", "action": "cancel_or_new_run",
                "row_attempt": int(row.get("task_attempt") or 0), "queue_attempt": int(task.get("_attempt") or 1)}
    hold = task.get(BUDGET_HOLD_KEY) if isinstance(task.get(BUDGET_HOLD_KEY), dict) else {}
    if hold.get("reason") == HOLD_MALFORMED_RESUME_IDENTITY:
        return {"ok": False, "error": HOLD_MALFORMED_RESUME_IDENTITY}
    live_grant = row.get("grant") if isinstance(row.get("grant"), dict) else {}
    if "grant" in row and not str(live_grant.get("grant_id") or "").strip():
        return {"ok": False, "error": "malformed_grant_identity"}
    if row.get("state") == STATE_RESUME_GRANTED and not live_grant.get("revoked_at"):
        # Orphaned = no queue carrier holds this undispatched grant any more. That
        # covers a typed revocation hold AND a crash between the durable grant and
        # the snapshot (restore saw only `_budget_pause`), which would otherwise
        # answer resume_already_granted forever (Astra run-a882315dbcd7 #1).
        orphaned = bool(
            not any(isinstance(item.get("_budget_pause_resume"), dict)
                        and str(item["_budget_pause_resume"].get("grant_id") or "") == str(live_grant.get("grant_id") or "")
                        for item in list(q.PENDING) + [m.get("task") for m in q.RUNNING.values() if isinstance(m, dict)]
                        if isinstance(item, dict))
        )
        if not orphaned:
            return {"ok": False, "error": "resume_already_granted",
                    "grant_id": live_grant.get("grant_id")}
        revoked = {**live_grant, "revoked_at": utc_now_iso(),
                   "revoke_reason": (f"deferred:{hold.get('reason')}:{str(hold.get('detail') or '')[:120]}"
                                     if hold else "orphaned_grant_without_carrier")}
        try:
            set_budget_pause(result_root, task_id, {**row, "state": STATE_PAUSED, "grant": revoked},
                             expected_pause_id=pause_id, expected_state=STATE_RESUME_GRANTED,
                             expected_grant_id=str(live_grant.get("grant_id") or ""))
        except Exception as exc:
            return {"ok": False, "error": HOLD_REVOCATION_UNWRITTEN, "detail": str(exc)[:200],
                    "grant_id": live_grant.get("grant_id"), "action": "retry_or_cancel"}
        row = {**row, "state": STATE_PAUSED, "grant": revoked}
    try:
        read_actor_source_bytes(result_root, task_id, row["source_ref"])
    except Exception:
        return {"ok": False, "error": "pause_source_unreadable", "action": "cancel_or_new_run"}
    # Custody observed outside the queue lock by the caller, else read now.
    if not isinstance(external, dict):
        external = observe_task_runs(result_root, task_id, reason="budget_resume_uncovered_cost")
    if external.get("custody_read") != "ok":
        return {"ok": False, "error": "external_custody_unreadable",
                "detail": str(external.get("error") or ""), "action": "retry_or_cancel"}
    # ``stop_confirmed`` is the only terminal fact; every other run stays under custody.
    unsettled = [run for run in (external.get("runs") or [])
                 if isinstance(run, dict) and str(run.get("state") or "") != EXTERNAL_STOP_CONFIRMED]
    if unsettled:
        try:
            set_budget_pause(result_root, task_id, {**row, "external_runs": external},
                             expected_pause_id=pause_id, expected_state=str(row.get("state") or ""))
        except Exception:
            log.debug("Fresh custody observation could not be recorded on %s", task_id, exc_info=True)
        return {"ok": False, "error": "external_runs_unsettled",
                "runs": [{key: run.get(key) for key in ("run_id", "state", "stop_outcome")} for run in unsettled],
                "action": "wait_for_delegated_runs_to_settle_or_cancel_them"}
    row = {**row, "external_runs": external}
    try:
        if has_active_intent(pathlib.Path(q.DRIVE_ROOT), task_id, strict=True):
            return {"ok": False, "error": "cancel_intent_active"}
    except Exception:
        return {"ok": False, "error": "cancellation_authority_unavailable"}
    deadline = parse_deadline_ts(task.get("deadline_at") or (task.get("task_contract") or {}).get("deadline_at"))
    if deadline is not None and deadline <= utc_now():
        return {"ok": False, "error": "deadline_passed"}
    now = time.time()
    started = float(row.get("started_at") or checkpoint.get("started_at") or 0.0)
    paused_at = float(row.get("paused_at") or checkpoint.get("paused_at") or now)
    prior_paused = float(row.get("paused_duration_sec") or 0.0)
    # ONE shared clock: wall time minus the quota union minus the paused carrier.
    executed_sec = execution_elapsed_seconds(
        {"started_at": started, "budget_paused_sec": prior_paused,
         "model_wait_quota_clock": row.get("model_wait_quota_clock") or {}}, paused_at)
    ceiling = get_task_abs_ceiling_sec()  # None = unlimited lifetime; 0 = exhausted
    if ceiling is not None and started and executed_sec >= float(ceiling):
        return {"ok": False, "error": "lifetime_exhausted", "executed_sec": round(executed_sec, 1)}
    try:
        # Authoritative, never the admit-only stale snapshot: a grant is money.
        remaining = budget_remaining(q.load_state(), strict=True, allow_stale=False)
    except Exception:
        return {"ok": False, "error": "monetary_authority_unavailable"}
    if remaining <= 0:
        return {"ok": False, "error": "budget_still_exhausted", "action": "increase_budget_then_resume"}
    root_task_id = str(pause.get("root_task_id") or task.get("root_task_id") or task_id)
    root_grant = live_root_resume_grant(q, root_task_id, result_root) if root_task_id != task_id else {}
    if selected_by and root_task_id != task_id:
        # Q9: lineage alone grants nothing; model selection needs this root's live grant.
        if not root_grant:
            return {"ok": False, "error": "root_resume_grant_missing",
                    "root_task_id": root_task_id, "action": "resume_root_first"}
    if str(pause.get("scope") or "") == "root":
        from ouroboros.usage_accounting import refresh_root_accounting

        # ONE fresh strict ledger read is this grant's monetary authority: it never
        # answers from the display cache, so a stale snapshot cannot pose as room.
        tree = refresh_root_accounting(result_root, root_task_id, strict=True)
        if not isinstance(tree, dict):
            # Unknown tree spend is not room: an unreadable ledger refuses typed.
            return {"ok": False, "error": "root_accounting_unavailable",
                    "action": "retry_or_cancel"}
        if tree.get("integrity_degraded"):
            return {"ok": False, "error": "root_accounting_degraded",
                    "action": "retry_or_cancel"}
        limit, accounted = tree.get("root_limit_usd"), tree.get("accounted_usd")
        if limit is not None:
            if accounted is None:
                return {"ok": False, "error": "root_accounting_degraded",
                        "action": "retry_or_cancel"}
            if float(accounted) >= float(limit) - 1e-9:
                return {"ok": False, "error": "root_hard_cap_exhausted",
                        "action": "increase_budget_then_resume"}
    # Owner Q9: a descendant cannot be resumed under a root that is itself still paused.
    if root_task_id != task_id and any(
            str(item.get("id") or "") == root_task_id and isinstance(item.get("_budget_pause"), dict)
            for item in q.PENDING):
        return {"ok": False, "error": "root_still_paused", "root_task_id": root_task_id,
                "action": "resume_root_first"}
    # A later Resume raises the generation; older grants cannot become live again.
    generation = int(row.get("resume_generation") or 0) + 1
    pause_generation = int(row.get("pause_generation") or 0)
    grant = {
        "grant_id": uuid.uuid4().hex, "granted_at": utc_now_iso(), "granted_at_ts": now,
        "single_use": True, "paused_duration_sec": prior_paused + max(0.0, now - paused_at),
        "executed_sec_before_pause": round(executed_sec, 3),
        "pause_id": pause_id, "pause_generation": pause_generation, "generation": generation,
        "selected_by": str(selected_by or "owner"),
        "root_grant_id": str(root_grant.get("grant_id") or ""),
        "root_resume_generation": int(root_grant.get("generation") or 0),
        "root_fence_id": (str((q.BUDGET_ROOT_FENCES.get(root_task_id) or {}).get("fence_id") or "")
                          if root_task_id != task_id else ""),
        "refresh_planning_threshold": str(row.get("rail") or "") in {
            "graceful_ceiling", "wrapup_last_fit", "soft_land"},
    }
    prior_pause = dict(pause)
    try:
        # CAS on the validated state: a concurrent writer refuses here, typed.
        set_budget_pause(result_root, task_id,
                         {**row, "state": STATE_RESUME_GRANTED, "grant": grant,
                          "resume_generation": generation},
                         expected_pause_id=pause_id, expected_state=str(row.get("state") or ""))
    except Exception as exc:
        return {"ok": False, "error": "grant_not_recorded", "detail": str(exc)[:200]}
    task.pop("_budget_pause", None)
    task["_budget_pause_resume"] = {
        **checkpoint, "grant_id": grant["grant_id"], "granted_at": grant["granted_at"],
        "grant_generation": generation, "pause_id": pause_id, "pause_generation": pause_generation,
        "paused_duration_sec": grant["paused_duration_sec"], "pause": prior_pause,
        "external_runs": external,
        **{key: grant[key] for key in ("selected_by", "root_grant_id", "root_resume_generation", "root_fence_id")},
    }
    task["budget_resumed_at"] = grant["granted_at"]
    # Release a re-validated restore/revocation hold in place (``selected`` flips,
    # nothing is erased); the prior hold is kept for the snapshot rollback below.
    released_hold = hold if hold and not hold.get("selected") else None
    if released_hold is not None:
        task[BUDGET_HOLD_KEY] = {**released_hold, "selected": True, "selected_at": utc_now_iso(),
                                 "selected_by": grant["selected_by"], "released_reason": "exact_resume_granted"}
    fence = q.BUDGET_ROOT_FENCES.get(root_task_id)
    fence_released = False
    held_siblings: List[str] = []
    released_markers: Dict[str, Dict[str, Any]] = {}
    rebound_holds: Dict[str, Dict[str, Any]] = {}
    if (task_id == root_task_id and isinstance(fence, dict) and str(fence.get("fence_id") or "")
            == str(prior_pause.get("fence_id") or fence.get("fence_id"))):
        # The root's own Resume lifts its admission latch: exact descendants keep
        # their OWN `_budget_pause` rows and are only ELIGIBLE; the model selects each (Q9).
        q.BUDGET_ROOT_FENCES.pop(root_task_id, None)
        fence_released = True
        held_siblings, released_markers, rebound_holds = hold_root_resume_descendants(q, root_task_id, fence, grant)
    if not q.persist_queue_snapshot(reason="budget_exact_resume_granted"):
        task.pop("_budget_pause_resume", None)
        task["_budget_pause"] = prior_pause
        if released_hold is not None:
            task[BUDGET_HOLD_KEY] = released_hold
        if fence_released:
            q.BUDGET_ROOT_FENCES[root_task_id] = fence
        for member in q.PENDING:
            member_id = str(member.get("id") or "")
            if member_id in set(held_siblings):
                member.pop(BUDGET_HOLD_KEY, None)
                if member_id in released_markers:
                    member["_budget_pause"] = released_markers[member_id]
            elif member_id in rebound_holds:
                member[BUDGET_HOLD_KEY] = rebound_holds[member_id]
        try:
            set_budget_pause(result_root, task_id, row, expected_pause_id=pause_id,
                             expected_state=STATE_RESUME_GRANTED,
                             expected_grant_id=str(grant["grant_id"]))
            return {"ok": False, "error": "snapshot_not_persisted"}
        except Exception as rollback_error:
            detail = str(rollback_error)[:120]
            log.warning("Exact resume grant rollback remains unpersisted for %s", task_id, exc_info=True)
        # Rollback failed: the hold keeps this orphaned grant's identity so the
        # next Resume writes its deferred revocation before minting.
        hold_budget_row(
            task, reason=HOLD_REVOCATION_UNWRITTEN,
            detail=f"snapshot_not_persisted:{detail}",
            extra={"pause_id": pause_id, "grant_id": str(grant["grant_id"]),
                   "root_task_id": root_task_id,
                   **({"prior_hold_reason": str(released_hold.get("reason") or "")}
                      if released_hold else {})},
            result_root=result_root)
        task["_budget_pause"] = prior_pause
        return {"ok": False, "error": "snapshot_not_persisted",
                "held": HOLD_REVOCATION_UNWRITTEN, "grant_id": str(grant["grant_id"])}
    try:
        from ouroboros.task_results import STATUS_SCHEDULED, write_task_result

        write_task_result(
            result_root, task_id, STATUS_SCHEDULED, reason_code="",
            resource_limit={**prior_pause, "status": "resume_granted", "resumed_at": grant["granted_at"],
                            "grant_id": grant["grant_id"], "auto_resume": False},
        )
    except Exception:
        log.debug("Failed to project exact budget resume for %s", task_id, exc_info=True)
    eligible = [str(r.get("id") or "") for r in q.PENDING
                if isinstance(r.get("_budget_pause"), dict) and r["_budget_pause"].get("exact_continuation")
                and str(r.get("root_task_id") or "") == root_task_id and str(r.get("id") or "") != task_id]
    q.append_jsonl(
        q.DRIVE_ROOT / "logs" / "events.jsonl",
        {"ts": utc_now_iso(), "type": "budget_task_explicitly_resumed", "task_id": task_id,
         "root_task_id": root_task_id, "same_generation": True, "exact_continuation": True,
         "grant_id": grant["grant_id"], "grant_generation": generation,
         "selected_by": grant["selected_by"],
         "paused_duration_sec": grant["paused_duration_sec"],
         "eligible_descendants": eligible if task_id == root_task_id else [],
         "held_siblings": held_siblings, "rebound_held_siblings": sorted(rebound_holds),
         "released_hold": str((released_hold or {}).get("reason") or "")},
    )
    return {"ok": True, "task_id": task_id, "root_task_id": root_task_id, "exact_continuation": True,
            "grant_id": grant["grant_id"], "grant_generation": generation,
            "paused_duration_sec": round(grant["paused_duration_sec"], 1),
            "eligible_descendants": eligible if task_id == root_task_id else [],
            "held_siblings": held_siblings, "rebound_held_siblings": sorted(rebound_holds),
            **({"released_hold": str(released_hold.get("reason") or "")} if released_hold else {})}


def revoke_exact_budget_resume(task: Dict[str, Any], reason: str) -> bool:
    """Return a granted-but-undispatched task to its exact pause (queue lock held).

    Money can vanish between the grant and the dispatch (a sibling spent it) and
    a restart may intervene; the grant is single-use and must not be dispatched
    into a refused send. Identity decides what may be written: a revocation is
    recorded ONLY against the pause, state and grant this handoff names
    (compare-and-set on all three). A grant the durable row says was CONSUMED
    is never re-armed: the task ran on, the queue row is stale — it takes a
    typed hold, its handoff leaves, and no ``_budget_pause`` marker is
    re-minted over a task that is not paused. If the durable row already
    carries a NEWER pause (pauseA -> Resume -> pauseB), or a different grant,
    nothing is written over it — the spent handoff simply leaves the queue
    row, which re-reads the current pause or holds; at restore, a newer grant
    that never reached a worker either is revoked too, so the next Resume finds
    a pause, not a grant no row carries. A failed write leaves the row
    un-dispatchable rather than carrying a stale grant.
    """
    from ouroboros.budget_pause import (
        LIVE_PAUSE_STATES, STATE_PAUSED, STATE_RESUME_GRANTED, STATE_RESUMED, budget_pause_row,
        exact_pause_marker, set_budget_pause,
    )
    from supervisor.events_budget import (
        HOLD_GRANT_CONSUMED_STALE_ROW, HOLD_RECORD_UNREADABLE_AT_REVOKE, HOLD_RESTART_REVOCATION_UNWRITTEN,
        HOLD_REVOCATION_UNWRITTEN, HOLD_STALE_GRANT_SUPERSEDED, HOLD_MALFORMED_RESUME_IDENTITY, hold_budget_row,
    )
    from supervisor import queue as q

    handoff = task.get("_budget_pause_resume") if isinstance(task.get("_budget_pause_resume"), dict) else None
    if handoff is None:
        return False
    task_id = str(task.get("id") or "")
    result_root = pathlib.Path(task.get("budget_drive_root") or q.DRIVE_ROOT)
    prior_pause = dict(handoff.get("pause") or {}) if isinstance(handoff.get("pause"), dict) else {}
    expected_pause_id = str(handoff.get("pause_id") or "").strip()
    handoff_grant_id = str(handoff.get("grant_id") or "")
    if not expected_pause_id or not handoff_grant_id.strip() or not prior_pause:
        task["_budget_pause"] = prior_pause
        hold_budget_row(task, reason=HOLD_MALFORMED_RESUME_IDENTITY, detail="malformed_resume_identity",
                        extra={"pause_id": expected_pause_id, "grant_id": handoff_grant_id}, result_root=None)
        return False
    try:
        row = budget_pause_row(result_root, task_id)
    except Exception:
        log.warning("Exact resume grant revocation could not read the pause row for %s",
                    task_id, exc_info=True)
        # The saved pause is retained: the marker stays the locator the owner's
        # next Resume validates; the hold keeps the row off the dispatch path.
        task["_budget_pause"] = prior_pause
        hold_budget_row(task, reason=HOLD_RECORD_UNREADABLE_AT_REVOKE,
                         detail=str(reason or ""),
                         extra={"pause_id": expected_pause_id, "grant_id": handoff_grant_id},
                         result_root=result_root)
        return False
    current_pause_id = str(row.get("pause_id") or "")
    row_state = str(row.get("state") or "")
    grant = dict(row["grant"]) if isinstance(row.get("grant"), dict) else {}
    if (not current_pause_id.strip() or (current_pause_id == expected_pause_id or row_state == STATE_RESUME_GRANTED)
            and not str(grant.get("grant_id") or "").strip()):
        task["_budget_pause"] = prior_pause
        hold_budget_row(task, reason=HOLD_MALFORMED_RESUME_IDENTITY, detail="malformed_durable_resume_identity",
                        extra={"pause_id": expected_pause_id, "grant_id": handoff_grant_id}, result_root=None)
        return False
    same_pause = current_pause_id == expected_pause_id
    same_grant = str(grant.get("grant_id") or "") == handoff_grant_id
    consumed = bool(grant.get("consumed_at")) or row_state == STATE_RESUMED
    if same_pause and same_grant and consumed:
        # NEVER re-armed: the loop consumed this grant, so the task ran (or
        # ran and ended). This queue row is a stale carrier; it is held typed,
        # off the dispatch path, with no pause marker — and a restore fences it
        # as the running work it names.
        task.pop("_budget_pause_resume", None)
        if isinstance(task.get("_owner_wait_resume"), dict):
            # The task ran ON past this grant and LATER parked in an owner wait:
            # the spent carrier is simply retired and the newer planned-restart
            # handoff decides the row (its own restore gate re-validates it).
            # Fencing the row here would drop that valid continuation (#1196, F3).
            q.append_jsonl(q.DRIVE_ROOT / "logs" / "events.jsonl",
                           {"ts": utc_now_iso(), "type": "budget_resume_carrier_retired",
                            "task_id": task_id, "reason": str(reason or ""),
                            "grant_id": str(grant.get("grant_id") or ""),
                            "pause_id": current_pause_id,
                            "consumed_at": grant.get("consumed_at"),
                            "retained": "owner_wait_resume"})
            return False
        task["_budget_pause_consumed"] = {
            "pause_id": current_pause_id, "grant_id": str(grant.get("grant_id") or ""),
            "consumed_at": grant.get("consumed_at"), "reason": str(reason or ""),
        }
        hold_budget_row(task, reason=HOLD_GRANT_CONSUMED_STALE_ROW, detail=str(reason or ""),
                         extra={"pause_id": current_pause_id, "grant_id": str(grant.get("grant_id") or "")},
                         result_root=None)  # the task's own status is not ours to rewrite
        q.append_jsonl(q.DRIVE_ROOT / "logs" / "events.jsonl",
                       {"ts": utc_now_iso(), "type": "budget_resume_grant_revoke_refused_consumed",
                        "task_id": task_id, "reason": str(reason or ""),
                        "grant_id": str(grant.get("grant_id") or ""), "pause_id": current_pause_id,
                        "consumed_at": grant.get("consumed_at")})
        return False
    superseded = not same_pause or not same_grant
    if superseded:
        log.warning("Stale exact-resume handoff for %s (grant %s, pause %s) is NOT written over the "
                    "current pause %s", task_id, handoff_grant_id, expected_pause_id,
                    current_pause_id or "<none>")
        task.pop("_budget_pause_resume", None)
        newer_undispatched = (
            str(reason or "") == "restart_before_dispatch"
            and row_state == STATE_RESUME_GRANTED
            and str(grant.get("grant_id") or "")
            and not grant.get("revoked_at") and not grant.get("consumed_at")
        )
        if newer_undispatched:
            # The snapshot lagged a NEWER grant; no worker survives a restart, so
            # that grant never reached one either. Revoked under its OWN
            # identity, or the row is held naming it (the next Resume writes
            # the deferred revocation before minting again).
            revoked = {**grant, "revoked_at": utc_now_iso(),
                       "revoke_reason": "restart_before_dispatch:superseding_grant_undispatched"}
            try:
                set_budget_pause(result_root, task_id, {**row, "state": STATE_PAUSED, "grant": revoked},
                                 expected_pause_id=current_pause_id, expected_state=STATE_RESUME_GRANTED,
                                 expected_grant_id=str(grant.get("grant_id") or ""))
                row = {**row, "state": STATE_PAUSED, "grant": revoked}
                row_state = STATE_PAUSED
            except Exception:
                log.warning("Superseding grant of %s could not be revoked at restore; held", task_id, exc_info=True)
                task["_budget_pause"] = exact_pause_marker(
                    row, default_root=str(task.get("root_task_id") or task_id))
                hold_budget_row(task, reason=HOLD_RESTART_REVOCATION_UNWRITTEN,
                                 detail=str(reason or ""),
                                 extra={"pause_id": current_pause_id, "grant_id": str(grant.get("grant_id") or "")},
                                 result_root=result_root)
                return False
        if row_state in LIVE_PAUSE_STATES and row.get("source_ref"):
            task["_budget_pause"] = exact_pause_marker(
                row, default_root=str(task.get("root_task_id") or task_id))
        else:
            hold_budget_row(task, reason=HOLD_STALE_GRANT_SUPERSEDED,
                             detail=str(reason or ""),
                             extra={"handoff_pause_id": expected_pause_id,
                                    "current_pause_id": current_pause_id},
                             result_root=result_root)
        q.append_jsonl(q.DRIVE_ROOT / "logs" / "events.jsonl",
                       {"ts": utc_now_iso(), "type": "budget_resume_grant_revoke_superseded",
                        "task_id": task_id, "reason": str(reason or ""),
                        "handoff_grant_id": handoff_grant_id,
                        "handoff_pause_id": expected_pause_id, "current_pause_id": current_pause_id,
                        "current_grant_id": grant.get("grant_id"),
                        "superseding_grant_revoked": bool(newer_undispatched)})
        return False
    try:
        grant.update(revoked_at=utc_now_iso(), revoke_reason=str(reason or ""))
        set_budget_pause(result_root, task_id, {**row, "state": STATE_PAUSED, "grant": grant},
                         expected_pause_id=current_pause_id, expected_state=row_state,
                         expected_grant_id=str(grant.get("grant_id") or ""))
    except Exception:
        log.warning("Exact resume grant revocation not recorded for %s", task_id, exc_info=True)
        # Retained, typed, un-dispatchable: the marker stays so the owner's next
        # Resume finds the exact pause; the grant named here is proven
        # undispatched (its handoff leaves the row with this hold), so that
        # Resume writes the deferred revocation before minting a new grant.
        task["_budget_pause"] = prior_pause
        hold_budget_row(task, reason=(HOLD_RESTART_REVOCATION_UNWRITTEN
                                      if str(reason or "") == "restart_before_dispatch"
                                      else HOLD_REVOCATION_UNWRITTEN),
                         detail=str(reason or ""),
                         extra={"pause_id": current_pause_id, "grant_id": grant.get("grant_id")},
                         result_root=result_root)
        return False
    task["_budget_pause"] = prior_pause
    task.pop("_budget_pause_resume", None)
    q.append_jsonl(q.DRIVE_ROOT / "logs" / "events.jsonl",
                   {"ts": utc_now_iso(), "type": "budget_resume_grant_revoked", "task_id": task_id,
                    "reason": str(reason or ""), "grant_id": grant.get("grant_id"),
                    "pause_id": current_pause_id})
    return True
