"""Turn-end outcome refresh for messaging-platform turns. [daveey]

``GatewayRunner._handle_message_with_agent`` refreshes the chat's stored outcome
(``hermes_state_outcome``) when a turn concludes, however it concluded. The refresh runs on a daemon
thread that inherits the turn's context (profile scope) and is never awaited: a judge call or a slow
store read must not hold the reply the adapter is about to send. Best-effort: never raises.
"""

from __future__ import annotations

import logging
import threading
from contextvars import ContextVar
from typing import Optional

logger = logging.getLogger(__name__)


run_outcome: ContextVar[Optional[dict]] = ContextVar("gateway_run_outcome", default=None)


def capture_run_outcome(agent) -> None:
    """Freeze worker-owned evidence before the cached agent can run again.

    The handler owns this dict; copied executor contexts share it, never the mutable session turn slot.
    """
    owned = run_outcome.get()
    if owned is not None:
        owned["lease_holder"] = getattr(agent, "_last_session_turn_lease_holder", None)
        owned["session_id"] = str(getattr(agent, "session_id", "") or owned["session_id"])


def concluding_turn_identity(runner, event=None, session_key: Optional[str] = None) -> frozenset:
    """The concluding gateway turn's OWN evidence: its routing ``active_turn_token`` (the event owns it until
    ``_clear_durable_active_turn``) and its agent's turn-lease holder. Never a snapshot of the chat's live
    evidence: a concurrent Desktop turn's marker, or a turn that starts before the unawaited refresh runs, stays
    a live turn. Nothing named = nothing discounted."""
    try:
        from hermes_state_outcome import turn_identity

        holder = (run_outcome.get() or {}).get("lease_holder")
        token = getattr(event, "_gateway_active_turn_token", None)
        return turn_identity(lease_holder=holder if isinstance(holder, str) else None,
                             gateway_token=token if isinstance(token, str) else None)
    except Exception:  # health: allow BLE001 -- best-effort; nothing named discounts nothing
        logger.warning("gateway turn identity capture failed for %s", session_key, exc_info=True)
        return frozenset()


def _refresh(sid: str, db, concluding_turn: frozenset) -> None:
    try:
        from hermes_state_outcome import refresh_outcome

        refresh_outcome(sid, "turn_end", db=db, concluding_turn=concluding_turn)
    except Exception:  # health: allow BLE001 -- the turn already concluded; a failed refresh is logged
        logger.warning("gateway turn-end outcome refresh failed for %s", sid, exc_info=True)


def refresh_gateway_turn_outcome(runner, session_entry, event=None,
                                 session_key: Optional[str] = None) -> Optional[threading.Thread]:
    """Start the refresh off the reply path; the started thread (None when there is nothing to refresh).
    The concluding turn's identity is taken here, before the thread is scheduled."""
    owned = run_outcome.get()
    sid = str(owned["session_id"] if owned is not None else getattr(session_entry, "session_id", "") or "")
    store = getattr(runner, "_session_db", None)
    db = getattr(store, "_db", store)
    if not sid or db is None:
        return None
    try:
        from agent.memory_provider import spawn_context_thread

        concluding = concluding_turn_identity(runner, event, session_key)
        thread = spawn_context_thread(_refresh, name="gateway-turn-outcome", args=(sid, db, concluding))
        thread.start()
        return thread
    except Exception:  # health: allow BLE001 -- a thread that can't start skips one refresh; logged
        logger.warning("gateway turn-end outcome refresh could not start for %s", sid, exc_info=True)
        return None
