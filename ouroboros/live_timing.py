"""Renewal timing assembly behind the admitted host port and its existing journal."""
import time

from ouroboros.contracts.realtime import canonical_json


def record_timing(host, event, *, at=None):
    """One journal fact per renewal; absent boundaries remain explicitly unknown."""
    stamp = time.time() if at is None else at
    current = getattr(host, "_renewal_timing", None)
    if event in {"requested", "closed"} and current is not None:
        host._renewal_timing = None
        host.observe("host", canonical_json({"type": "call.timing", **current,
            "ended_at": stamp, "ended_by": event}))
    if event == "requested":
        host._renewal_number = getattr(host, "_renewal_number", 0) + 1
        host._renewal_timing = {"renewal": host._renewal_number, "requested_at": stamp,
            "quiet_at": None, "fit_done_at": None, "provider_ready_at": None, "first_audio_at": None}
        return
    if event == "closed" or current is None:
        return
    field = {"quiet": "quiet_at", "fit_done": "fit_done_at", "ready": "provider_ready_at",
             "first_audio": "first_audio_at"}.get(event)
    if field is None:
        return
    # A requested renewal may still hear the retiring connection before replacement fit/ready.
    if ((event == "ready" and current["fit_done_at"] is None)
            or (event == "first_audio" and current["provider_ready_at"] is None)):
        return
    # An account rebind can fit twice before open: the last completed fit is the sent one.
    if current[field] is None or event == "fit_done":
        current[field] = stamp
    if event == "first_audio":
        host._renewal_timing = None
        host.observe("host", canonical_json({"type": "call.timing", **current}))
