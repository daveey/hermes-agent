"""Turn-end outcome refresh for Desktop/TUI turns. [daveey]

Every conclusion of a prompt turn (complete, error, interrupt, refused prepare) refreshes the chat's
stored outcome (``hermes_state_outcome``) in the session's own profile store, so the sidebar paints
from what the turn left behind rather than from idleness. Best-effort: never raises into the turn.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def refresh_turn_outcome(session: dict, agent=None) -> None:
    """Refresh the outcome of the row this turn wrote (the agent's compression tip, else the session key)."""
    sid = str(getattr(agent, "session_id", "") or session.get("session_key") or "")
    if not sid:
        return
    try:
        from hermes_state_outcome import refresh_outcome
        from tui_gateway import server  # split helpers only work rebound onto server globals (bind_module)

        with server._session_db(session) as db:
            if db is not None:
                refresh_outcome(sid, "turn_end", db=db)
    except Exception:  # health: allow BLE001 -- the turn already concluded; a failed refresh is logged
        logger.warning("turn-end outcome refresh failed for %s", sid, exc_info=True)
