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

:func:`denied_target` is the same backstop for a card hub admission denied at claim time (not worker-first, so its
``runs_on`` is written locally and pushed like any field, by the hub's claim screen).
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
    logger.warning("kanban dispatcher self-check: repinned %s to %s (%s)", task_id, labels, reason)


DENIED_REASON = ("dispatcher self-check: ready {minutes} min here after hub admission denied it ({why}); queued for "
                 "the worker pool (what `hermes kanban move --pool` does)")


def denied_target(conn, row, gateway, cfg, *, board, now=None):
    """``(labels, reason)`` for a ready card hub admission denied that still waits here with no ``runs_on``, else
    None. Read-only (cached, fresh machine registry only).

    Incident (11-13, hermes-hub): the claim screen denied t_01a184cee94603eb at 01:40Z but kept it, because its
    wake_check named a fleet probe under ``~/.hermes/bin`` that every worker also installs; the verdict was never
    revisited, so the hub claimed and released it for ~3 h while workers had free slots. The kept rule is fixed in
    :func:`hermes_cli.kanban_hub_admission_claim.keep_reason`; this backstop covers the rest: a ready, label-less
    (``placement_policy`` NULL) card created here, with no claim or lease, whose latest ``hub_admission`` event is a
    non-forced deny, ready for :data:`STALE_S`, that no keep reason holds for *now* (a recorded ``kept`` that still
    holds, e.g. a dir workspace, ``host_repo`` or a hub-local wake_check, keeps it), and whose worker-queue labels a
    live machine other than this one carries. The caller writes the same ``runs_on`` the claim-time deny route
    writes (``hermes kanban move --pool``'s labels) and a ``moved`` event with the reason.

    The verdict is :func:`hermes_cli.kanban_hub_admission_pickup.verdict`, the one the claim screen gates on: the
    latest ``hub_admission`` event, else the ``created`` payload's audit (``record_created`` mirrors that audit as an
    event, so the fallback only covers a card whose event is missing). The keep reasons (regexes on the card plus
    one indexed wake_check read) run before :func:`ready_since`, which scans the card's events and runs: this runs
    every tick for each judged card still here, and a card kept for good never needs its history read."""
    from hermes_cli import kanban_hub_admission_claim as claim
    from hermes_cli import kanban_hub_admission_pickup as pickup
    from hermes_cli import kanban_move as km
    from hermes_cli import kanban_parent_unclaimed as unclaimed
    from hermes_cli import kanban_worker_node as node

    # created_on NULL is this gateway's own card the Board has not stamped yet (kanban_sync_client "mine").
    if (row["placement_policy"] is not None or row["runs_on"] not in (None, "", "[]") or row["status"] != "ready"
            or row["created_on"] not in (None, gateway) or row["claim_lock"] is not None
            or row["lease_holder"] is not None):
        return None
    verdict = pickup.verdict(conn, row["id"])
    if not verdict or verdict.get("admit") is not False or verdict.get("forced"):
        return None
    fields = pickup._fields(row, board)
    if claim.keep_reason(conn, row, fields, cfg):
        return None
    waited = (time.time() if now is None else now) - unclaimed.ready_since(conn, row["id"], row["created_at"])
    if waited < STALE_S:
        return None
    labels = pickup._target(fields, gateway, cfg, dict(verdict))
    if not labels:
        return None
    registry = node.registry(require_fresh=True)
    if registry is None or not km.matching_machines(labels, registry.get("machines") or {}, me=gateway):
        return None
    why = str(verdict.get("why") or "no reason recorded")[:200]
    if verdict.get("kept"):
        why = f"{why}; kept for '{verdict['kept']}', which no longer holds"
    return labels, DENIED_REASON.format(minutes=int(waited // 60), why=why)
