"""Carry capable dispatcher verdicts omitted by machine-lane queue positions. [daveey]"""
from __future__ import annotations

import logging

from hermes_cli import kanban_dispatch_skip as skips

_log = logging.getLogger(__name__)


def fields(conn, task_id, place, ready_seconds, read_pacing):
    """Newest capable verdict in this ready episode, without replacing a queue-provided skip.

    Ordering verdicts remain ordinary capacity advice. Recorder-only eligibility failures are not verdicts on the
    card's capable workers. A recorded pacing verdict survives even when this emitter cannot read the governor.
    """
    if place.get("skip"):
        return {}
    verdict = next((skip for skip in skips.episode_skips(conn, task_id) if not skips.recorder_cannot_lease(skip)), None)
    if not verdict or skips.is_ordering(verdict):
        return {}
    carried = skips.notice_fields({"skip": verdict}, ready_seconds)
    if verdict.get("reason") != skips.LLM_PACED:
        return carried
    admission = {"reason": skips.LLM_PACED, "status": "held", "providers": verdict.get("providers", [])}
    try:
        current = read_pacing(conn, task_id)
        if current:
            admission = {**current, "reason": skips.LLM_PACED}
    except Exception:  # health: allow BLE001 -- keep the recorded verdict if the advisory governor read fails
        _log.debug("notice: pacing read unavailable for %s", task_id, exc_info=True)
    return {**carried, "admission": admission}
