"""Dispatcher self-check: a worker-first card left with no ``runs_on`` joins the worker queue. [daveey]

Incident (11-13, hermes-hub): ``kanban_create`` enrolled t_01a1d2a8ea3dfbdb worker-first (``runs_on`` NULL) after
hub admission denied it, because the sync loop's runstate still read "stopped" from the previous gateway during the
restarted loop's 20-minute first round. The Board never assigned it and its reserves failed on the dispatch network
budget, so it sat ready 36 min while pool workers claimed 63 other cards, until a manual ``hermes kanban move --pool``.

The root cause is fixed at the loop (:func:`hermes_cli.kanban_sync_runstate.started`). This is the backstop for any
other way a card lands there: a ready ``worker-first-v1`` card created on this gateway, with no ``runs_on``, no claim
and no lease, ready for :data:`STALE_S` while this gateway has a worker queue label that a live machine carries for
the card's labels, gets the same Board repin ``hermes kanban move --pool`` makes (``[<pool label>, *coordination
labels]``, so ``requires: [hub:write]`` keeps ``capability:hub-write``). :func:`hermes_cli.kanban_worker_placement.
reevaluate` makes the repin in place of a reserve; the card's ``moved`` event records why.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

STALE_S = 300
REASON = ("dispatcher self-check: ready {minutes} min as a worker-first card with no runs_on; queued for the worker "
          "pool (what `hermes kanban move --pool` does)")


def pool_target(conn, row, gateway, cfg, *, now=None):
    """``(labels, reason)`` for a stranded worker-first card, else None. Read-only; no network beyond the cached
    machine registry, which must be fresh (an unreadable registry waits for the next tick)."""
    from hermes_cli import kanban_move as km
    from hermes_cli import kanban_placement as placement
    from hermes_cli import kanban_worker_node as node
    from hermes_cli import kanban_worker_repin as repin
    from hermes_cli.kanban_parent_unclaimed import ready_since
    from hermes_cli.kanban_worker_placement import POLICY

    if (row["placement_policy"] != POLICY or row["runs_on"] is not None or row["status"] != "ready"
            or row["created_on"] != gateway or row["claim_lock"] is not None or row["lease_holder"] is not None):
        return None
    if placement._Cfg(cfg).queue_label(gateway) != placement.POOL_LABEL:
        return None  # no worker queue here (or another label): worker-first is this gateway's route
    waited = (time.time() if now is None else now) - ready_since(conn, row["id"], row["created_at"])
    if waited < STALE_S:
        return None
    labels = repin._pool_target(row)
    if labels is None:
        return None  # an action no pool worker is labelled for keeps its worker-first placement
    registry = node.registry(require_fresh=True)
    if registry is None or not km.matching_machines(labels, registry.get("machines") or {}, me=gateway,
                                                    exclude_me=False):
        return None
    return labels, REASON.format(minutes=int(waited // 60))


def log_repin(task_id, labels, reason):
    logger.warning("kanban worker-first self-check: repinned %s to %s (%s)", task_id, labels, reason)
