"""Turn-end outcome refresh for messaging-platform turns. [daveey]

``GatewayRunner._handle_message_with_agent`` refreshes the chat's stored outcome
(``hermes_state_outcome``) when a turn concludes, however it concluded, while the turn's profile
scope is still bound. The read runs off the event loop. Best-effort: never raises into the handler.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def refresh_gateway_turn_outcome(runner, session_entry) -> None:
    sid = str(getattr(session_entry, "session_id", "") or "")
    store = getattr(runner, "_session_db", None)
    db = getattr(store, "_db", store)
    if not sid or db is None:
        return
    try:
        from hermes_state_outcome import refresh_outcome

        await asyncio.to_thread(refresh_outcome, sid, "turn_end", db=db)
    except Exception:  # health: allow BLE001 -- the turn already concluded; a failed refresh is logged
        logger.warning("gateway turn-end outcome refresh failed for %s", sid, exc_info=True)
