"""Bound headless notice turns without killing their background work.

Websocket detachments retain their own reconnect/interrupt timer policy.
"""
from threading import Timer
from time import monotonic
import logging

from tui_gateway._env import env_float

GRACE_S = max(0.0, env_float("HERMES_TUI_DETACHED_TURN_GRACE_S", 60.0))
logger = logging.getLogger(__name__)


def _headless(session):
    from tui_gateway import server
    return session.get("transport") is server._stdio_transport and not server._stdio_is_rpc_channel


def cancel(session):
    """Caller holds history_lock (or has exclusively popped the session)."""
    timer = session.pop("_detached_turn_timer", None)
    if timer is not None:
        timer.cancel()


def settled(session: dict, *, only_if_missing=False) -> None:
    """A new idle turn gets a fresh grace; protected agents keep pollers, not slots."""
    from tui_gateway import server
    # Match resume's history -> sessions order, never wait for history while
    # holding the registry lock. A resumed/rebound record is revalidated here.
    with session["history_lock"], server._sessions_lock:
        sid = next((sid for sid, live in server._sessions.items() if live is session), None)
        if sid is None or session.get("running") or not _headless(session):
            return
        if only_if_missing and session.get("_detached_turn_timer") is not None:
            return
        if not server._session_is_reapable(sid, session):
            server._release_active_session_slot(session)
        cancel(session)
        _arm(sid, session, GRACE_S)


def repair():
    """Reaper safety net for failed dispatches and lost timer callbacks."""
    from tui_gateway import server
    with server._sessions_lock:
        sessions = list(server._sessions.values())
    for session in sessions:
        if _headless(session):
            settled(session, only_if_missing=True)


def _arm(sid: str, session: dict, delay: float) -> None:
    from tui_gateway import server
    deadline = monotonic() + delay

    def reap():
        popped = None
        # Same resume fence and pop/teardown split as WS orphan cleanup: slow
        # finalization must run outside locks (agent.close can join a turn).
        with server._session_resume_lock, session["history_lock"], server._sessions_lock:
            if server._sessions.get(sid) is not session or session.get("_detached_turn_timer") is not timer:
                return
            session.pop("_detached_turn_timer", None)
            if session.get("running") or not _headless(session):
                return
            remaining = deadline - monotonic()
            if remaining > 0:
                _arm(sid, session, remaining)
            elif server._session_is_reapable(sid, session):
                popped = server._pop_session_by_id(sid)
            else:
                server._release_active_session_slot(session)
                _arm(sid, session, max(1.0, GRACE_S))
        if popped is not None:
            server._teardown_popped_session(popped, end_reason="ws_orphan_reap")

    def guarded_reap():
        try:
            reap()
        except Exception:
            logger.exception("detached turn cleanup failed for %s", sid)
            # repair() will retry a transient failure on the next reaper scan.
            with session["history_lock"]:
                if session.get("_detached_turn_timer") is timer:
                    session.pop("_detached_turn_timer", None)

    timer = Timer(delay, guarded_reap)
    timer.daemon = True
    session["_detached_turn_timer"] = timer
    timer.start()
