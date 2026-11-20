"""Retry durable queued prompts after an update hold or transient contention."""
import logging
import threading
import time


def _warn_stranded_queue(sid, session):
    from hermes_cli.backend_retirement import last_refusal, update_hold_started_at
    head = session.get("queued_prompt") or {}
    if head.get("queued_reason") != "core_update":
        return
    if update_hold_started_at(session.get("profile_home") or None) is not None:
        session.pop("_queue_unheld", None)
        return
    now = time.monotonic()
    observed = session.get("_queue_unheld")
    if observed is None or observed[0] is not head:
        observed = session["_queue_unheld"] = [head, now, False]
    if not observed[2] and now - observed[1] >= 60:
        observed[2] = True
        logging.getLogger(__name__).warning(
            "update-hold queue for session %s remains unadmitted 60s after hold lift (refusal=%s)",
            sid, last_refusal())


def retry_queued_prompt(rid, sid, session):
    """Called under history_lock; retain the envelope, not an admitted turn.

    One timer per session. Each attempt re-enters the normal admission fence.
    Backend shutdown/Stop may discard the queue; the callback checks it afresh.
    """
    from hermes_cli.backend_retirement import retirement
    if session.get("_closing") or session.get("_finalized") or not session.get("queued_prompt"):
        return False
    from tui_gateway import server
    _warn_stranded_queue(sid, session)
    if retirement.acquire():
        retirement.release()
    elif not server._update_held_queue(session):
        return False  # non-update queues retain the retirement refusal contract
    if retirement.retired():
        from tui_gateway.turn_marker import read_turn_marker, release_update_queue
        home, key = server._session_home(session), session.get("session_key")
        if key and (marker := read_turn_marker(home, key)):
            release_update_queue(home, key, marker)
        return False
    if session.get("_admission_retry") is not None:
        return True

    def retry():
        from tui_gateway import server
        with session["history_lock"]:
            session.pop("_admission_retry", None)
            if (session.get("_closing") or session.get("_finalized") or session.get("running")
                    or not session.get("queued_prompt")):
                return
        try:
            server._drain_queued_prompt(rid, sid, session)
        finally:
            # Admission can refuse without scheduling another attempt (retirement,
            # a racing running turn, or another caller's guard). Queue custody,
            # not the refusal reason, owns this retry chain.
            with session["history_lock"]:
                if not session.get("running"):
                    retry_queued_prompt(rid, sid, session)

    timer = threading.Timer(0.25, retry)
    timer.daemon = True
    session["_admission_retry"] = timer
    try:
        timer.start()
    except BaseException:
        session.pop("_admission_retry", None)
        raise
    return True


def redrive_update_queues(home):
    """Restart lost timers and release orphaned custody, even on an old backend.

    Resume serialization prevents adoption between the inventory and release.
    A live PID without a live queue must not indefinitely block startup replay.
    """
    import os
    from pathlib import Path
    from tui_gateway import server
    from tui_gateway.turn_marker import (
        UPDATE_HOLD_QUEUE_REASON, _load, _marker_path, release_update_queue,
    )

    # Inventory markers first: session.create does not take the resume lock. A
    # queue created during the disk read must appear in the later session snapshot.
    from contextlib import ExitStack
    home = Path(home)
    with server._session_resume_lock:
        markers = _load(_marker_path(home))
        with server._sessions_lock:
            sessions = list(server._sessions.items())
        for key, marker in markers.items():
            if (marker.get("reason") != UPDATE_HOLD_QUEUE_REASON
                    or marker.get("writer_pid") != os.getpid() or marker.get("queue_released")):
                continue
            owned = [(sid, session) for sid, session in sessions
                     if server._session_lookup_key(session, fallback=sid) == key
                     and Path(server._session_home(session)).resolve() == home.resolve()]
            with ExitStack() as locks:
                for _, session in owned:
                    locks.enter_context(session["history_lock"])
                # One stale alias must not release another alias's live custody.
                active = False
                for sid, session in owned:
                    if session.get("running") or session.get("_compute_host_turn_id"):
                        active = True  # claimed head can precede marker restamping
                    elif session.get("queued_prompt") and not (session.get("_closing") or session.get("_finalized")):
                        active = True
                        retry_queued_prompt("__update_queue__", sid, session)
                if not active:
                    release_update_queue(home, key, marker)


def discard_update_queue_marker(session):
    """Explicit queue cancellation must not become an orphan replay. Caller holds history_lock."""
    from tui_gateway import server
    from tui_gateway.turn_marker import UPDATE_HOLD_QUEUE_REASON, clear_turn_marker, read_turn_marker
    from hermes_state_outcome import turn_identity
    import os
    home, key = server._session_home(session), session.get("session_key")
    marker = read_turn_marker(home, key) if key else None
    if (marker and marker.get("reason") == UPDATE_HOLD_QUEUE_REASON
            and marker.get("writer_pid") == os.getpid()):
        clear_turn_marker(home, key, concluding_turn=turn_identity(marker=marker))

