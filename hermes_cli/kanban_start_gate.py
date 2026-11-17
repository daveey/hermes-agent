"""Plugin start gate: a plugin may hold a Kanban card start for a reason core does not know. [daveey]

The dispatcher decides starts from slots, routes, walls and the quota governor. Whether the fleet is over a token
budget is a plugin's knowledge (the token throttle of the hermes-mods throughput plugin): holding model calls
there while the dispatcher keeps starting cards only fills worker slots with runs that wait. So right before a
claim the dispatcher asks the ``kanban_start_gate`` hook. A callback answers ``{"hold": True, "reason": "<word>"}``
to keep the card ready and unclaimed this tick; anything else lets it start.

* The card is recorded like any other guard hold: ``respawn_guarded`` with reason ``start_gate:<word>`` and a
  clock hold. It is asked again every tick; nothing is persisted, so the hold ends when the plugin stops saying so.
* Fail open: no subscriber, a raising callback, a malformed answer or a ``hold`` that is not exactly ``True``
  starts the card.
* Callbacks run inline in the dispatcher tick for every candidate card: they must answer from a cached read.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

HOOK = "kanban_start_gate"
REASON_PREFIX = "start_gate:"
_WORD = re.compile(r"[^a-z0-9_]+")


def _reason(raw: Any) -> str:
    word = _WORD.sub("_", str(raw or "").strip().lower()).strip("_")[:40]
    return REASON_PREFIX + (word or "plugin")


def hold(*, task_id: str, lane: str, assignee: Optional[str], board: Optional[str]) -> Optional[str]:
    """``start_gate:<reason>`` when a plugin holds this start, else None."""
    try:
        from hermes_cli.lifecycle import has_hook, invoke_hook

        if not has_hook(HOOK):
            return None
        for answer in invoke_hook(HOOK, task_id=task_id, lane=lane, assignee=assignee, board=board):
            if isinstance(answer, dict) and answer.get("hold") is True:
                return _reason(answer.get("reason"))
    except (RuntimeError, ValueError, TypeError, LookupError, AttributeError, OSError) as exc:  # fail open
        logger.warning("kanban start gate: hook failed (%s: %s); starting ungated", type(exc).__name__, exc)
    return None
