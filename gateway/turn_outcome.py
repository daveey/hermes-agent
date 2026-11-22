"""Turn-end outcome refresh for messaging-platform turns. [daveey]

``GatewayRunner._handle_message_with_agent`` refreshes the chat's stored outcome
(``hermes_state_outcome``) when a turn concludes, however it concluded. The refresh runs on a daemon
thread that inherits the turn's context (profile scope) and is never awaited: a judge call or a slow
store read must not hold the reply the adapter is about to send. Best-effort: never raises.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

logger = logging.getLogger(__name__)


# The handler's custody record (``_handle_message_with_agent``): its default receipt, and under "concluding" the
# receipt of the last ``_run_agent`` invocation it started (a queued follow-up's recursion starts later).
run_outcome: ContextVar[Optional[dict]] = ContextVar("gateway_run_outcome", default=None)
# This ``_run_agent`` invocation's own receipt; its executor worker writes here through the copied context.
run_receipt: ContextVar[Optional[dict]] = ContextVar("gateway_run_receipt", default=None)


def begin_run_receipt(session_id):
    """Open a receipt for one ``_run_agent`` invocation and make it the handler's concluding one.

    A predecessor's worker keeps writing only its own receipt, so a late write after its follow-up started
    cannot rename the concluding turn. Returns the token for ``run_receipt.reset``."""
    receipt = {"session_id": str(session_id or "")}
    custody = run_outcome.get()
    if custody is not None:
        custody["concluding"] = receipt
    return run_receipt.set(receipt)


@contextmanager
def run_receipt_scope(session_id):
    """``begin_run_receipt`` for the duration of one ``_run_agent`` invocation."""
    token = begin_run_receipt(session_id)
    try:
        yield
    finally:
        run_receipt.reset(token)


def concluding_receipt() -> Optional[dict]:
    """The receipt of the handler's concluding run (its own default receipt when no run started)."""
    custody = run_outcome.get()
    if custody is None:
        return None
    return custody.get("concluding") or custody


def capture_run_outcome(agent, *, abandoned: bool = False) -> None:
    """Freeze worker-owned evidence before the cached agent can run again.

    Writes this run's receipt (the handler's own outside any ``_run_agent``), never the mutable session turn slot.
    ``abandoned``: the worker is still running, so only a lease it holds now is this run's; the cached agent's
    remembered holder may still be its previous turn's (abandonment before lease acquisition)."""
    owned = run_receipt.get() or run_outcome.get()
    if owned is None:
        return
    attr = "_active_session_turn_lease_holder" if abandoned else "_last_session_turn_lease_holder"
    owned["lease_holder"] = getattr(agent, attr, None)
    owned["session_id"] = str(getattr(agent, "session_id", "") or owned["session_id"] or "")


def concluding_turn_identity(event=None) -> frozenset:
    """The concluding gateway turn's OWN evidence: its routing ``active_turn_token`` (the event owns it until
    ``_clear_durable_active_turn``) and its run's turn-lease holder. Never a snapshot of the chat's live
    evidence: a concurrent Desktop turn's marker, or a turn that starts before the unawaited refresh runs, stays
    a live turn. Nothing named = nothing discounted."""
    try:
        from hermes_state_outcome import turn_identity

        holder = (concluding_receipt() or {}).get("lease_holder")
        token = getattr(event, "_gateway_active_turn_token", None)
        return turn_identity(lease_holder=holder if isinstance(holder, str) else None,
                             gateway_token=token if isinstance(token, str) else None)
    except Exception:  # health: allow BLE001 -- best-effort; nothing named discounts nothing
        logger.warning("gateway turn identity capture failed", exc_info=True)
        return frozenset()


def finish_gateway_turn_outcome(runner, session_entry, event, session_key, outcome_token, env_tokens) -> None:
    """Refresh in the concluding run's scope, then restore both scopes even on failure."""
    try:
        refresh_gateway_turn_outcome(runner, session_entry, event, session_key)
    finally:
        run_outcome.reset(outcome_token)
        runner._clear_session_env(env_tokens)


def refresh_gateway_turn_start_outcome(runner, session_entry) -> None:
    """Publish admission in the caller's profile scope before transcript/provider work."""
    sid = str(getattr(session_entry, "session_id", "") or "")
    store = getattr(runner, "_session_db", None)
    db = getattr(store, "_db", store)
    if not sid or db is None:
        return
    try:
        from hermes_state_outcome import refresh_outcome

        refresh_outcome(sid, "turn_start", db=db)
    except Exception:  # health: allow BLE001 -- publication failure never refuses an admitted turn
        logger.warning("gateway turn-start outcome refresh failed for %s", sid, exc_info=True)


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
    owned = concluding_receipt()
    sid = str((owned["session_id"] if owned is not None else getattr(session_entry, "session_id", "")) or "")
    store = getattr(runner, "_session_db", None)
    db = getattr(store, "_db", store)
    if not sid or db is None:
        return None
    try:
        from agent.memory_provider import spawn_context_thread

        concluding = concluding_turn_identity(event)
        thread = spawn_context_thread(_refresh, name="gateway-turn-outcome", args=(sid, db, concluding))
        thread.start()
        return thread
    except Exception:  # health: allow BLE001 -- a thread that can't start skips one refresh; logged
        logger.warning("gateway turn-end outcome refresh could not start for %s", sid, exc_info=True)
        return None
