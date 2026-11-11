"""Turn-end outcome refresh for Desktop/TUI turns. [daveey]

Every conclusion of a prompt turn (complete, error, interrupt, refused prepare) refreshes the chat's
stored outcome (``hermes_state_outcome``) in the session's own profile store, so the sidebar paints
from what the turn left behind rather than from idleness. Best-effort: never raises into the turn.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _turn_sid(session: dict, agent=None) -> str:
    return str(getattr(agent, "session_id", "") or session.get("session_key") or "")


def capture_concluding_turn(session: dict, agent=None) -> frozenset:
    """The concluding turn's own live-turn evidence (marker, turn lease), taken now. A refresh that runs later
    (a timer) passes it back so a turn started in between stays a live turn. Failure discounts nothing."""
    sid = _turn_sid(session, agent)
    if not sid:
        return frozenset()
    try:
        from hermes_state_outcome import capture_turn_identity
        from tui_gateway import server  # split helpers only work rebound onto server globals (bind_module)

        with server._session_profile_runtime_scope(session), server._session_db(session) as db:
            return capture_turn_identity(sid, db=db) if db is not None else frozenset()
    except Exception:  # health: allow BLE001 -- best-effort; an unreadable store discounts nothing
        logger.warning("turn identity capture failed for %s", sid, exc_info=True)
        return frozenset()


def refresh_turn_outcome(session: dict, agent=None, concluding_turn: Optional[frozenset] = None) -> None:
    """Refresh the outcome of the row this turn wrote (the agent's compression tip, else the session key).
    ``concluding_turn``: evidence captured when the turn concluded (``capture_concluding_turn``); omitted = the
    turn is concluding right now and its evidence is captured here, in the same store scope."""
    sid = _turn_sid(session, agent)
    if not sid:
        return
    try:
        from hermes_state_outcome import capture_turn_identity, refresh_outcome
        from tui_gateway import server  # split helpers only work rebound onto server globals (bind_module)

        # The turn has already released its secret scope (the home override alone
        # still exists). Outcome judges may call a provider, including on copied-
        # context threads: bind the owning session's full runtime scope again.
        with server._session_profile_runtime_scope(session), server._session_db(session) as db:
            if db is not None:
                if concluding_turn is None:
                    concluding_turn = capture_turn_identity(sid, db=db)
                refresh_outcome(sid, "turn_end", db=db, concluding_turn=concluding_turn)
    except Exception:  # health: allow BLE001 -- the turn already concluded; a failed refresh is logged
        logger.warning("turn-end outcome refresh failed for %s", sid, exc_info=True)
