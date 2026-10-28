"""[daveey] Background-job receipts at backend exit (t_01a182eec0f3584b).

A chat that starts ``terminal(background=true, notify=true)`` and ends its turn is idle, so a Hermes
update's step-boundary pause (``update_pause.boundary_pause_hook``) never sees it. When the backend then
exits (skew retirement, the updater's restart, idle exit, a Desktop quit), ``_shutdown_sessions`` closes
every session and ``agent.close`` kills its jobs with ``consume_output=True``: the notify completion the
chat was waiting for never arrives and nothing records why.

Before sessions are closed, each session's owned non-persist running jobs are journaled into its durable
turn marker (write-ahead), stopped with source ``backend_shutdown`` and ``consume_output=False``, and
their outcome journaled. A turn in flight keeps its crash marker and gains the list. An idle session gets
a marker of its own (reason ``background_jobs_stopped``) only when one of the jobs was ``notify=true``:
that notice would have woken the chat, so the next backend's auto-continue (startup sweep or
``session.resume``) submits a turn carrying the list and the normal completion notices. A real session
end (/new, archive, close) does not come through here and keeps today's behaviour.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SHUTDOWN_SOURCE = "backend_shutdown"
SHUTDOWN_KILL_REASON = "killed when the Hermes backend shut down"
JOBS_REASON = "background_jobs_stopped"
JOBS_PROMPT = "[Report background jobs stopped when the Hermes backend shut down]"
# The report is a delivery, not a turn to redo: it outlives the turn freshness window, up to the
# marker file's own retention.
JOBS_RECEIPT_MAX_AGE_S = 24 * 3600.0


def shutdown_kill_reason() -> str:
    """The update's reason when the checkout moved under this process or an update is live."""
    from tui_gateway import update_pause as up
    try:
        from hermes_cli.update_lock import update_in_progress
        if update_in_progress() or up._detect_skew() is not None:
            return up.UPDATE_KILL_REASON
    except Exception:
        logger.debug("backend exit: update state unreadable", exc_info=True)
    return SHUTDOWN_KILL_REASON


def jobs_stopped_note(killed_jobs: list | None) -> str:
    from tui_gateway.update_pause import killed_jobs_report
    note = ("[System note: the Hermes backend shut down while background jobs from this chat were running. "
            "No turn was in progress; this stands in for their completion notices. Check what each job "
            "was gating before you continue, and tell the user if it matters to them.]")
    report = killed_jobs_report(killed_jobs, "Background jobs stopped when the Hermes backend shut down:")
    return f"{note}\n\n{report}" if report else note


def journal_session_jobs(session: dict, reason: str) -> int:
    """Journal, stop and re-journal one session's jobs; returns how many were journaled."""
    from tui_gateway import update_pause as up
    from tui_gateway.turn_marker import mark_turn_paused, read_turn_marker, record_killed_jobs, record_turn_start
    key = str(session.get("session_key") or "")
    jobs = up._update_jobs(session) if key else []
    if not jobs:
        return 0
    home = up._home(session)
    marker = read_turn_marker(home, key)
    if marker is None:
        if not any(job.get("notify_on_complete") for job in jobs):
            return 0  # nothing was going to report back: today's close applies
        record_turn_start(home, key, JOBS_PROMPT)
        if not mark_turn_paused(home, key, JOBS_REASON, increment=False):
            logger.warning("backend exit: no receipt marker for session %s; its jobs end unreported", key)
            return 0
        merged = jobs
    else:
        merged = list({j["session_id"]: j for j in (*marker.get("killed_jobs", []), *jobs)}.values())

    def save() -> bool:
        return record_killed_jobs(home, key, merged)

    if not save():
        logger.warning("backend exit: could not journal background jobs of session %s", key)
        return 0
    if not up.stop_owned_jobs_for_update_pause(jobs, save, source=SHUTDOWN_SOURCE, reason=reason):
        logger.warning("backend exit: some background jobs of session %s have an unverified outcome", key)
    logger.info("backend exit: session %s journaled %d background job(s) (%s)", key, len(jobs), reason)
    return len(jobs)


def journal_background_jobs_before_exit() -> int:
    """Every live session's receipts, before ``_close_session_by_id`` would kill the jobs silently."""
    from tui_gateway import server
    with server._sessions_lock:
        sessions = list(server._sessions.values())
    reason = shutdown_kill_reason()
    total = 0
    for session in sessions:
        try:
            total += journal_session_jobs(session, reason)
        except Exception:
            logger.warning("backend exit: background-job journal failed for a session", exc_info=True)
    return total
