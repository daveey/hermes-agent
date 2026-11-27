"""Turn-end outcome refresh for Desktop/TUI turns. [daveey]

Every conclusion of a prompt turn (complete, error, interrupt, refused prepare) refreshes the chat's
stored outcome (``hermes_state_outcome``) in the session's own profile store, so the sidebar paints
from what the turn left behind rather than from idleness. Best-effort: never raises into the turn.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

_start_refresh_lock = threading.Lock()
_start_refresh_pending: dict[tuple[str, str], dict] = {}

logger = logging.getLogger(__name__)


def turn_sid(session: dict, agent=None) -> str:
    return str(getattr(agent, "session_id", "") or session.get("session_key") or "")


def own_turn_marker(session: dict, marker_key: str) -> Optional[dict]:
    """The marker this turn just recorded, read back right after ``record_turn_start`` (before any other turn of
    the chat can overwrite the key). Its identity is what the turn-end refresh may discount, nothing later."""
    if not marker_key:
        return None
    try:
        from tui_gateway import server  # split helpers only work rebound onto server globals (bind_module)
        from tui_gateway.turn_marker import read_turn_marker

        return read_turn_marker(server._session_home(session), marker_key)
    except Exception:  # health: allow BLE001 -- unreadable = not named, so the marker reads live (fail safe)
        logger.warning("own turn marker read failed for %s", marker_key, exc_info=True)
        return None


def concluding_turn_identity(agent=None, marker: Optional[dict] = None) -> frozenset:
    """The concluding TUI turn's OWN evidence: the marker it recorded (``own_turn_marker``) and its agent's
    turn-lease holder. Never a snapshot of what the chat holds now: a turn admitted after ``settle_prompt_state``,
    or a concurrent turn's marker or routing token, stays a live turn. Nothing named = nothing discounted."""
    try:
        from hermes_state_outcome import turn_identity

        # ``_last`` is assigned at admission and kept after release.
        holder = getattr(agent, "_last_session_turn_lease_holder", None)
        return turn_identity(marker=marker if isinstance(marker, dict) else None,
                             lease_holder=holder if isinstance(holder, str) else None)
    except Exception:  # health: allow BLE001 -- best-effort; nothing named discounts nothing
        logger.warning("turn identity capture failed", exc_info=True)
        return frozenset()


def refresh_turn_outcome(session: dict, agent=None, concluding_turn: Optional[frozenset] = None,
                         *, session_id: Optional[str] = None) -> None:
    """Refresh the outcome of the row this turn wrote (the agent's compression tip, else the session key).
    ``concluding_turn``: the turn's own evidence (``concluding_turn_identity``); omitted = nothing discounted."""
    sid = turn_sid(session, agent) if session_id is None else session_id
    if sid:
        _refresh_session_outcome(session, sid, "turn_end", concluding_turn or frozenset())


def refresh_turn_start_outcome(session: dict, agent=None) -> None:
    """Schedule admitted live evidence; board projection must never gate the prompt.

    Coalesce by profile home + admitted SID. Capture both before handing off: the
    cached session/agent may be reused or compressed while a projection is stuck.
    The worker reads current evidence, so a delayed start cannot resurrect an
    ended turn. Turn-end publication retains its existing synchronous ordering.
    """
    sid = turn_sid(session, agent)
    if not sid:
        return
    from hermes_cli.local_work_threads import TrackedThread
    from tui_gateway import server

    snapshot = session.copy()
    home = server._session_home(snapshot)
    # None/absent means launch profile: do not turn it into a foreign-home scope.
    key = (str(home), sid)
    with _start_refresh_lock:
        if pending := _start_refresh_pending.get(key):
            pending.update(snapshot=snapshot, dirty=True)
            return
        pending = {"snapshot": snapshot, "dirty": False}
        _start_refresh_pending[key] = pending

    def publish():
        try:
            while True:
                with _start_refresh_lock:
                    current = pending["snapshot"]
                    pending["dirty"] = False
                _refresh_session_outcome(current, sid, "turn_start", frozenset())
                with _start_refresh_lock:
                    if not pending["dirty"]:
                        del _start_refresh_pending[key]
                        return
        finally:
            with _start_refresh_lock:
                if _start_refresh_pending.get(key) is pending:
                    del _start_refresh_pending[key]

    try:
        TrackedThread(target=publish, name="turn-start-outcome", daemon=True,
                      home=home, source="tui.turn_start_outcome").start()
    except Exception:  # health: allow BLE001 -- rejected background admission must not abort the prompt
        with _start_refresh_lock:
            if _start_refresh_pending.get(key) is pending:
                del _start_refresh_pending[key]
        logger.warning("turn_start outcome scheduling failed for %s", sid, exc_info=True)


def _refresh_session_outcome(session: dict, sid: str, trigger: str, concluding: frozenset) -> None:
    try:
        from hermes_state_outcome import refresh_outcome
        from tui_gateway import server  # split helpers only work rebound onto server globals (bind_module)

        # The turn has already released its secret scope (the home override alone
        # still exists). Outcome judges may call a provider, including on copied-
        # context threads: bind the owning session's full runtime scope again.
        with server._session_profile_runtime_scope(session), server._session_db(session) as db:
            if db is not None:
                refresh_outcome(sid, trigger, db=db, concluding_turn=concluding)
    except Exception:  # health: allow BLE001 -- outcome publication must never block a turn
        logger.warning("%s outcome refresh failed for %s", trigger, sid, exc_info=True)


def refresh_closed_session_outcome(session: dict) -> None:
    """In-memory close/reap has no DB orphan sweep. Refresh so a later good reply is stored.

    Hub chats 20261120_041627_3ea0c6 / d40002 / 2e950a: ``ws_orphan_reap`` tore down the live
    session after a later complete turn; without this, stored ``unresolved{turn_failed}`` from
    the earlier failed_turn row survived until a manual ``hermes sessions outcome --refresh``.
    """
    # A held continuation marker belongs to pending recovery, not a completed reply.
    # Its writer is still this live backend after reap; an ordinary refresh would
    # overwrite the turn-end result with ``running``. Preserve that stored result.
    if any(session.get(key) for key in (
        "_refused_turn_marker_key", "_update_pause_marker_key", "_paused_marker_carried",
    )):
        return
    sid = turn_sid(session, session.get("agent"))
    if not sid:
        return
    try:
        from hermes_state_outcome import refresh_outcomes_quietly
        from tui_gateway import server  # split helpers only work rebound onto server globals (bind_module)

        with server._session_profile_runtime_scope(session), server._session_db(session) as db:
            if db is not None:
                refresh_outcomes_quietly([sid], "reap", db=db)
    except Exception:  # health: allow BLE001 -- the session is already closed; a failed refresh is logged
        logger.warning("closed-session outcome refresh failed for %s", sid, exc_info=True)
