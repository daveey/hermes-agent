"""Plugin start gate: a plugin may hold a Kanban card start for a reason core does not know. [daveey]

The dispatcher decides starts from slots, routes, walls and the quota governor. Whether the fleet is over a token
budget is a plugin's knowledge (the token throttle of the hermes-mods throughput plugin): holding model calls
there while the dispatcher keeps starting cards only fills worker slots with runs that wait. So right before a
claim the dispatcher asks the ``kanban_start_gate`` hook. A callback answers ``{"hold": True, "reason": "<word>"}``
to keep the card ready and unclaimed this tick; anything else lets it start.

* The tick reports the card under ``respawn_guarded`` with reason ``start_gate:<word>`` and records a
  ``dispatch_skipped`` skip (reason ``start_gate``, detail ``<word>``; ``kanban_dispatch_skip``), so wait notices
  and diagnostics name the gate instead of "skip reason unrecorded" or a stranded card. The dispatcher-stuck
  detector counts it as an expected hold (``kanban_dispatch_expected_holds``). It is not a respawn-guard hold: no
  guard-hold cap, no auto-block, nothing counted against the card. The card is asked again every tick and starts
  when no callback holds it.
* Pickup asks too (``kanban_worker_node._paced``): a machine whose gate holds imports no new foreign cards it would
  only hold, the same way it skips cards its quota governor would hold.
* Fail open: no subscriber, a raising callback, a plugin-discovery fault, a malformed answer or a ``hold`` that
  is not exactly ``True`` starts the card. A fault is logged once per process, not once per card.
* Callbacks run inline in the dispatcher tick for every candidate card: they must answer from a cached read.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

HOOK = "kanban_start_gate"
SKIP_REASON = "start_gate"  # the ``dispatch_skipped`` reason; the word is its detail
REASON_PREFIX = SKIP_REASON + ":"
_warned = False
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
    except Exception as exc:  # health: allow BLE001 -- a plugin or discovery fault never holds a card (fail open)
        global _warned
        if not _warned:
            _warned = True
            logger.warning("kanban start gate: hook failed (%s: %s); starting ungated", type(exc).__name__, exc)
    return None


def word(reason: Any) -> Optional[str]:
    """``<word>`` of a ``start_gate:<word>`` guard reason, else None."""
    text = str(reason or "")
    return text[len(REASON_PREFIX):] if text.startswith(REASON_PREFIX) and len(text) > len(REASON_PREFIX) else None


def pre_claim_hold(conn: Any, row: Any, lane: str, result: Any, *, assignee: Optional[str], board: Optional[str],
                   dry_run: bool = False) -> bool:
    """The dispatcher's last two waits before a claim: the quota governor (``kanban_llm_pacing_dispatch.hold``, which
    records its own event) and then the plugin start gate, recorded as a guard hold on ``result``."""
    from hermes_cli.kanban_llm_pacing_dispatch import hold as pacing_hold

    if pacing_hold(conn, row, lane, result, dry_run=dry_run):  # every account of the pool at reserve
        return True
    reason = hold(task_id=row["id"], lane=lane, assignee=assignee, board=board)
    if reason is None:
        return False
    result.respawn_guarded.append((row["id"], reason))  # asked again next tick; nothing is persisted
    return True
