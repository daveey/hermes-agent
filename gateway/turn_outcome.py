"""Turn-end outcome refresh for messaging-platform turns. [daveey]

``GatewayRunner._handle_message_with_agent`` refreshes the chat's stored outcome
(``hermes_state_outcome``) when a turn concludes, however it concluded. The refresh runs on a daemon
thread that inherits the turn's context (profile scope) and is never awaited: a judge call or a slow
store read must not hold the reply the adapter is about to send. Best-effort: never raises.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


def _capture(sid: str, db) -> frozenset:
    """The concluding turn's own live-turn evidence (routing token, turn lease, marker), taken now, before the
    refresh is scheduled. The refresh runs later on an unawaited thread: a newer turn may have started by then,
    and only this turn's evidence may be discounted. Nothing captured = nothing discounted."""
    try:
        from hermes_state_outcome import capture_turn_identity

        return capture_turn_identity(sid, db=db)
    except Exception:  # health: allow BLE001 -- best-effort; an unreadable store discounts nothing
        logger.warning("gateway turn identity capture failed for %s", sid, exc_info=True)
        return frozenset()


def _refresh(sid: str, db, concluding_turn: frozenset) -> None:
    try:
        from hermes_state_outcome import refresh_outcome

        refresh_outcome(sid, "turn_end", db=db, concluding_turn=concluding_turn)
    except Exception:  # health: allow BLE001 -- the turn already concluded; a failed refresh is logged
        logger.warning("gateway turn-end outcome refresh failed for %s", sid, exc_info=True)


def refresh_gateway_turn_outcome(runner, session_entry) -> Optional[threading.Thread]:
    """Start the refresh off the reply path; the started thread (None when there is nothing to refresh)."""
    sid = str(getattr(session_entry, "session_id", "") or "")
    store = getattr(runner, "_session_db", None)
    db = getattr(store, "_db", store)
    if not sid or db is None:
        return None
    try:
        from agent.memory_provider import spawn_context_thread

        thread = spawn_context_thread(
            _refresh, name="gateway-turn-outcome", args=(sid, db, _capture(sid, db)))
        thread.start()
        return thread
    except Exception:  # health: allow BLE001 -- a thread that can't start skips one refresh; logged
        logger.warning("gateway turn-end outcome refresh could not start for %s", sid, exc_info=True)
        return None
