"""[daveey] Background-job receipts at backend exit (t_01a182eec0f3584b).

A chat that starts ``terminal(background=true, notify=true)`` and ends its turn is idle, so a Hermes
update's step-boundary pause (``update_pause.boundary_pause_hook``) never sees it. When the backend then
exits (skew retirement, the updater's restart, idle exit, a Desktop quit), ``_shutdown_sessions`` closes
every session and ``agent.close`` kills its jobs with ``consume_output=True``: the notify completion the
chat was waiting for never arrives and nothing records why.

Pending native completion events also ride this journal, with their actual exit status and output
(``kill_state=completed``): held for the chat, queued, in flight in a poller (registry custody,
``process_registry_awaiting.pending_events``), or claimed by the registry but not yet published (the event
the claim built, ``unpublished_completions``). No step waits on a timer: the snapshot reads custody, and the
exit fence (raised under ``_sessions_lock``) makes the turn's worker start the single commit point for a notice
turn a poller claimed before the fence: started earlier → delivered (``_completion_delivered``) and, until that
turn's crash marker is on disk, still journaled (``COMPLETION_UNSECURED``; at least once, possibly twice if the
marker lands between the two reads); not started yet → the start is refused, the event stays in custody and is
journaled, never also delivered here. Admission alone (ownership, slot) commits nothing. Finished jobs are never killed or offered restart advice. A user Stop before the exit,
``off`` notifications, consumed/delivered completions and other chats' events are not journaled. This is an
orderly-exit snapshot, not crash durability or historical result replay.

Before sessions are closed, each session's owned non-persist running jobs are journaled into its durable
turn marker (write-ahead), stopped with source ``backend_shutdown`` and ``consume_output=False``, and
their outcome journaled. A turn in flight keeps its crash marker and gains the list. An idle session gets
a marker of its own (reason ``background_jobs_stopped``) only when one of the jobs was ``notify=true``:
that notice would have woken the chat, so the next backend's auto-continue (startup sweep or
``session.resume``) submits a turn carrying the list and the normal completion notices. A real session
end (/new, archive, close) does not come through here and keeps today's behaviour.
"""

from __future__ import annotations

import contextlib
import logging

logger = logging.getLogger(__name__)

SHUTDOWN_SOURCE = "backend_shutdown"
SHUTDOWN_KILL_REASON = "killed when the Hermes backend shut down"
JOBS_REASON = "background_jobs_stopped"
JOBS_PROMPT = "[Report background job results pending when the Hermes backend shut down]"
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
    note = ("[System note: background job results from this chat were pending when the Hermes backend "
            "shut down. This stands in for their completion notices. Check what each job "
            "was gating before you continue, and tell the user if it matters to them.]")
    report = killed_jobs_report(killed_jobs, "Background job results at backend shutdown:")
    return f"{note}\n\n{report}" if report else note


# Whether the user had stopped the chat before the exit began. ``_stop_turns_before_exit`` sets
# ``_turn_cancel_requested`` with reason ``backend_exit`` too, so the live flag alone cannot tell a user Stop
# (suppresses automatic wakes until the next prompt) from the shutdown's own interrupt.
USER_STOP_AT_EXIT = "_user_stop_at_exit"
EXIT_FENCE = "_exit_notices_pending"


def fence_exit_notices(sessions=None) -> None:
    """Before anything interrupts a turn: record each session's user-Stop state, then fence automatic wakes so a
    notice journaled for the next backend is not also delivered by this one. One-way; only the exit calls it."""
    from tui_gateway import server
    # Raised only under ``_sessions_lock`` (reentrant): a notice turn's start commits under it
    # (``start_synthesized_turn``), so each start lands wholly before the fence or is refused after it. Lock order
    # is ``history_lock`` -> ``_sessions_lock`` (session.resume holds history_lock while it takes _sessions_lock),
    # so each session is fenced under its own history_lock first.
    with server._sessions_lock:
        if sessions is None:
            sessions = list(server._sessions.items())
    for _sid, session in sessions:
        with session.get("history_lock") or contextlib.nullcontext(), server._sessions_lock:
            session.setdefault(USER_STOP_AT_EXIT, bool(session.get("_turn_cancel_requested")))
            session[EXIT_FENCE] = True


def _user_stopped(session: dict) -> bool:
    return bool(session.get(USER_STOP_AT_EXIT, session.get("_turn_cancel_requested")))


def _owns_pending(sid: str, session: dict, event: dict) -> bool:
    """The live poller's routing, not a narrower filter: an addressed event belongs to the session that provably
    owns it with no more precise live owner elsewhere; an ownerless legacy event to the session whose agent
    spawned it (the ids ``close()`` kills by)."""
    from tui_gateway import server
    if server._notification_event_requires_owner(event):
        return (server._session_owns_notification_event(sid, session, event)
                and server._notification_event_owner_elsewhere(sid, session, event) is None)
    return str(event.get("owner_task_id") or "") in server._session_process_owners(session)


def _pending_completions(sid: str, session: dict) -> list[dict]:
    """Native completion events still owed to this chat: claimed but unpublished, queued, in flight in a poller,
    or held for it. Never historical result files, never UI-emission dedup. Caller fenced the session first."""
    from tui_gateway import server
    from tools.process_registry import process_registry, _redact_process_result
    from tools.process_registry_awaiting import pending_events, unpublished_completions
    from tools.process_registry_notifications import format_process_notification
    if _user_stopped(session) or server._background_notifications_off(session):
        return []
    # Custody order (each move keeps the event in its old place until it is in the new one): unpublished →
    # queue ↔ in flight ↔ held. The held list is read under its lock together with queue and in-flight, so a
    # poller moving an event between them (take_ready, _notif_take_held, a requeue) is seen at least once.
    events = unpublished_completions(process_registry)
    from tui_gateway import notification_park
    # Read every off-queue custodian together with queue/in-flight custody. Separate
    # snapshots can miss a queue-to-park move between reads. Park claims acquire an
    # in-flight hold before removing their source copy. No callbacks under these locks.
    with process_registry._lock, notification_park._lock, server._notif_held_lock:
        events.extend(process_registry._parked_events or ())
        events.extend(evt for _deadline, evt in notification_park._parked)
        events.extend(pending_events(process_registry.completion_queue))
        events.extend(session.get("_notif_held") or ())
    # Stable after the fence: a notice turn's start marks its notices delivered under ``_sessions_lock``, and
    # no notice turn starts once the fence is set. A started turn whose crash marker is not on disk yet still
    # owes them (``COMPLETION_UNSECURED``): reported here too; the marker, once written, carries the notice.
    from tui_gateway.session_notifications import COMPLETION_UNSECURED
    with session.get("history_lock") or contextlib.nullcontext():
        unsecured = list((session.get(COMPLETION_UNSECURED) or {}).values())
        delivered = set(session.get("_completion_delivered", ())) - {e.get("session_id") for e in unsecured}
    events.extend(unsecured)
    jobs = {}
    for event in events:
        pid = event.get("session_id") if isinstance(event, dict) else None
        if (not pid or pid in jobs or event.get("type", "completion") != "completion"
                or process_registry.is_completion_consumed(pid) or pid in delivered
                or not _owns_pending(sid, session, event)):
            continue
        receipt = _redact_process_result(dict(event))
        receipt.update(kill_state="completed", notify_on_complete=True,
                       notice=format_process_notification(receipt) or "")
        jobs[pid] = receipt
    return list(jobs.values())


def _merge(prior: list, jobs: list) -> list:
    receipts = {j["session_id"]: j for j in prior}
    for job in jobs:
        # SIGTERM then atexit sees the completion emitted by our own kill: keep its verified
        # shutdown receipt (reason/restart advice), not a second notice.
        if receipts.get(job["session_id"], {}).get("kill_state") == "killed" and job.get("kill_state") == "completed":
            continue
        receipts[job["session_id"]] = job
    return list(receipts.values())


def _save_receipts(home, key: str, jobs: list) -> bool:
    """Merge ``jobs`` into the session's marker, creating a receipt marker only when something would have
    reported back. False = nothing durable (no marker and no notify job, or the write failed)."""
    from tui_gateway.turn_marker import merge_receipts
    ok = merge_receipts(home, key, jobs, _merge, create=any(job.get("notify_on_complete") for job in jobs),
                        prompt=JOBS_PROMPT)
    if not ok and any(job.get("notify_on_complete") for job in jobs):
        logger.warning("backend exit: no receipt marker for session %s; its jobs end unreported", key)
    return ok


def journal_session_jobs(session: dict, reason: str, *, sid: str = "") -> int:
    """Journal, stop and re-journal one session's running jobs, then journal its pending completions;
    returns how many were journaled."""
    from tui_gateway import update_pause as up
    key = str(session.get("session_key") or "")
    if not key:
        return 0
    home = up._home(session)
    running = up._update_jobs(session)
    journaled = {}
    if running:
        if _save_receipts(home, key, running):  # write-ahead, before any signal
            if not up.stop_owned_jobs_for_update_pause(running, lambda: _save_receipts(home, key, running),
                                                        source=SHUTDOWN_SOURCE, reason=reason):
                logger.warning("backend exit: some background jobs of session %s have an unverified outcome", key)
            journaled.update((job["session_id"], job) for job in running)
        elif any(job.get("notify_on_complete") for job in running):
            logger.warning("backend exit: could not journal background jobs of session %s", key)
    # After the kills: a job that exited on its own between the running snapshot and its kill
    # (``already_exited``) has claimed its finish by now or is still in ``_running`` (its uncertain receipt
    # stands). A claimed one's real completion is in custody and supersedes that receipt.
    pending = _pending_completions(sid, session)
    if pending:
        if _save_receipts(home, key, pending):
            journaled.update((job["session_id"], job) for job in pending)
        else:
            logger.warning("backend exit: could not journal pending completions of session %s", key)
    if journaled:
        logger.info("backend exit: session %s journaled %d background job(s) (%s)", key, len(journaled), reason)
    return len(journaled)


def journal_background_jobs_before_exit() -> int:
    """Every live session's receipts, before ``_close_session_by_id`` would kill the jobs silently."""
    from tui_gateway import server
    with server._sessions_lock:
        sessions = list(server._sessions.items())
    # Idempotent: _stop_turns_before_exit normally fenced first. Every session is fenced before any is read,
    # so no notice turn anywhere starts after its event could have been journaled.
    fence_exit_notices(sessions)
    # Pollers wind down on their own; nothing here waits for them. Their in-flight and held events are read
    # from custody, and a turn they claimed before the fence cannot start after it.
    for stop in {session.get("_notif_stop") for _sid, session in sessions} - {None}:
        stop.set()
    reason = shutdown_kill_reason()
    total = 0
    for sid, session in sessions:
        try:
            total += journal_session_jobs(session, reason, sid=sid)
        except Exception:
            logger.warning("backend exit: background-job journal failed for a session", exc_info=True)
    return total
