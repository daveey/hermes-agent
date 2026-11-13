"""Worker-first placement honours the pool's pickup gate (:func:`kanban_placement_coordination.claim_refusal`). [daveey]

Incident t_01a1d560c6d3bc3c (11-13, MacBook default board): two cards quoted the hub CLI's task-list command in
backticks as evidence. :func:`kanban_placement_coordination.requirements` reads that as a home-only coordination
action, so every pool worker's pickup gate refused them, yet creation enrolled them worker-first and the origin's
placement tick reserved a worker about every 10 s (lease_version 1519 and 642) for 42 and 22 minutes. Nobody ever
ran them, and ``kanban move --pool`` was refused too; only rewording the bodies freed them.

Creation no longer enrolls such a card (``creation_decision`` refuses a card whose ``required_labels`` is None), but a
card enrolled by a gateway still on older code, or one whose body was edited after enrollment to quote a home action,
is still enrolled. Two entry points share :func:`kanban_placement_coordination.pool_refusal` (the gate's
machine-state-independent verdict over the whole pool):

* the placement tick (:func:`origin_target`): a card already enrolled is never reserved again. It gets one
  ``capability_mismatch`` queue reason with a comment quoting the matched text, and an unstarted ready card is
  repinned to its origin through the Board's creator repin (what ``hermes kanban move --runs-on machine:<origin>``
  does, :func:`kanban_worker_repin.eligibility`);
* the queue reason (:func:`refusals`): a Board reply never lists a candidate as eligible when that machine's
  pickup gate refuses the card (label mismatches included: annotation only, the Board keeps routing those).
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

REPIN_WHY = ("every pool worker's pickup gate refuses it ({reason}: `{quote}`); repinned to its origin {origin} "
             "(what `hermes kanban move --runs-on {origin}` does) instead of reserving a worker that never claims it")
COMMENT = ("No pool worker can claim this card: every candidate's pickup gate refuses it ({reason}: `{quote}`), so "
           "its origin reserves none. An unstarted ready card is repinned to its origin {origin} (`hermes kanban "
           "move {task} --runs-on {origin}`); otherwise reword the text or move the card.")
QUOTE_CAP = 200


def _gate(card, pool):
    """The pool-wide refusal this module acts on, or None. Only the machine-state-independent verdicts (text the
    gate reads as a home-only action, malformed ``requires``): the Board cannot read card text, so it reserves such
    a card forever. A label mismatch is the Board's own structural ``origin`` return ("no pool worker can run it:
    missing ..."), from the attested registry it owns; it stays there."""
    from hermes_cli import kanban_placement_coordination as coordination
    if not isinstance(pool, list):
        return None
    gate = coordination.pool_refusal(card, pool)
    if gate is None or gate["reason"] not in (coordination.ACTION_REASON, coordination.INVALID_REQUIRES):
        return None
    return {**gate, "quote": str(gate["quote"] or "")[:QUOTE_CAP]}


def refusals(row, pool):
    """:func:`kanban_placement_coordination.pickup_refusals` for a reserve's queue reason; ``{}`` on any fault
    (a diagnostic never controls dispatch)."""
    from hermes_cli import kanban_placement_coordination as coordination
    try:
        return coordination.pickup_refusals(dict(row), pool if isinstance(pool, list) else [])
    except Exception:  # health: allow BLE001 -- an annotation must not suppress a reserve or its record
        logger.warning("pickup gate annotation failed for %s", row["id"], exc_info=True)
        return {}


def origin_target(conn, row, gateway, pool, *, refused_at=None):
    """None when some member of ``pool`` may claim ``row`` (reserve as today). Otherwise ``(labels, why)``:
    ``labels`` is ``[machine:<gateway>]`` when the origin repin applies now, or None when nothing may be asked this
    tick (started card, or the Board refused that repin at ``refused_at``, this revision). Either way the caller
    makes no reserve. The queue reason is recorded once per card."""
    from hermes_cli import kanban_queue_reason as queue
    from hermes_cli import kanban_worker_repin as repin
    from hermes_cli.kanban_worker_placement import POLICY

    if row["placement_policy"] != POLICY:
        return None
    try:
        gate = _gate(dict(row), pool)
    except Exception:  # health: allow BLE001 -- a gate fault keeps today's reserve; it never stops the tick
        logger.warning("pickup gate check failed for %s", row["id"], exc_info=True)
        return None
    if gate is None:
        return None
    origin = f"machine:{gateway}"
    queue.try_record(record_mismatch, conn, row["id"], gateway, gate, origin)
    why = REPIN_WHY.format(origin=origin, reason=gate["reason"], quote=gate["quote"])
    if refused_at == row["sync_version"]:
        return None, why
    if not repin.eligibility(conn, row["id"], [origin]).get("ok"):
        return None, why
    return [origin], why


def record_mismatch(conn, task_id, gateway, gate, origin):
    """Append the ``capability_mismatch`` observation and its one comment, unless this card already has the same
    one for its current lifecycle (any earlier event, not only the latest: a Board refusal between ticks must not
    make it a new transition). True when written."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_queue_reason as queue

    machines = gate["machines"]
    value = {"version": 1, "code": "capability_mismatch",
             "candidates": [{"machine": name, "reasons": [queue.PICKUP_REFUSED],
                             **({"missing_labels": machines[name]["missing_labels"]}
                                if machines[name].get("missing_labels") else {})} for name in sorted(machines)],
             "pickup_gate": {"reason": gate["reason"], "quote": gate["quote"]}}
    with kb.write_txn(conn):
        row = queue._owned_row(conn, task_id, gateway, None)
        if row is None:
            return False
        value["lifecycle"] = queue._lifecycle(conn, task_id, row)
        mine = queue._signature(value)
        for (payload,) in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind=? AND json_valid(payload) "
                "AND json_extract(payload, '$.code')='capability_mismatch'", (task_id, queue.EVENT)):
            if queue._signature(json.loads(payload)) == mine:
                return False
    comment = COMMENT.format(reason=gate["reason"], quote=gate["quote"], origin=origin, task=task_id)
    return queue._record(conn, task_id, gateway, value, comment=comment)
