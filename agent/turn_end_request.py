"""Ask exactly one running turn to end, by its durable turn-lease holder. [daveey]

A Stop typed in one window for a turn Hermes started itself in ANOTHER process (the gateway's Kanban notice or
close-out turn) used to take the chat over: the registry hand-off made that turn's lease watcher hard-cancel it as
``session_turn_lease_lost``. Instead the asking process records an end request for the exact holder string
(``SessionDB.request_turn_end``: a compare-and-set against ``session_turn_leases``, so it can only name the turn
that holds the lease now), and the holding process's ``DurableTurnLease`` tick sees it and calls the agent's
ordinary interrupt. The turn ends like a Stop in its own window: no lease loss, the lease is released as usual,
and a prompt queued behind it in the gateway's FIFO runs next, once.

Only automated turns are targeted. The turn's holder string names its kind (``:auto=<display kind>``, see
:func:`auto_token`), so the asker never ends a turn somebody typed. A gateway that honours requests says so with
``CAPABILITY`` in its runtime identity; with an older gateway the caller keeps its fallback.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from agent.context_compressor_turns import TURN_STARTING_NOTICE_KINDS

#: The runtime-identity capability of a gateway whose turns honour end requests.
CAPABILITY = "turn-end-request-v1"
#: Turns Hermes starts on its own (``tui_gateway.session_lifecycle.AUTOMATED_TURN_KINDS``): the turn-starting
#: notices (not a /loop tick the user started) and a paused turn continuing after an update.
AUTO_KINDS = frozenset((TURN_STARTING_NOTICE_KINDS - {"loop_tick"}) | {"auto_continue"})
#: What the ending turn reports as its interrupt message.
END_MESSAGE = "Stopped from another Hermes window."
# Only the segment right before the LAST ``:platform=``: ``lease_holder`` puts it there, after the turn id, whose
# session/task parts a client may choose (an ``:auto=`` inside them must not count).
_AUTO_RE = re.compile(r":auto=([a-z_]+)$")


def auto_token(display_kind: Any) -> str:
    """The ``:auto=<kind>`` fragment for a turn whose user row is an automated kind, else ``""``."""
    return f":auto={display_kind}" if isinstance(display_kind, str) and display_kind in AUTO_KINDS else ""


def holder_auto_kind(holder: Any) -> Optional[str]:
    """The automated kind a durable holder string names, else None (a typed turn, or an older holder)."""
    head, sep, _platform = str(holder or "").rpartition(":platform=")
    match = _AUTO_RE.search(head) if sep else None
    return match.group(1) if match and match.group(1) in AUTO_KINDS else None
