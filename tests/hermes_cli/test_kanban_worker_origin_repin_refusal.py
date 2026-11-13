"""A Board-refused self-check pool repin must not suppress the origin repin at that revision. [daveey]

Review t_01a1d5dba1ad82b9 of PR #2362 (t_01a1d560c6d3bc3c): ``kanban_worker_placement`` kept one
``_selfcheck_refused`` entry per card and passed it to ``kanban_worker_pickup_gate.origin_target`` as ``refused_at``.
A stranded worker-first card whose self-check pool repin the Board refused, and that every pool worker's pickup gate
refuses, was then never asked to go home (``--runs-on machine:<origin>``) at that revision: no reserve, one
``capability_mismatch`` comment, stranded until the revision moved. Entry path: the origin's placement tick
(``kanban_worker_placement.reevaluate``) on a stranded card whose wire row carries a malformed ``requires`` (the
self-check reads its access labels; every pickup gate refuses it as invalid).
"""
from __future__ import annotations

import json

from hermes_cli import kanban_placement_coordination as coord
from hermes_cli import kanban_queue_reason as queue
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_pool_deny_runs_on as deny

hub = deny.hub
hub_judge = deny.hub_judge
no_pull = deny.no_pull
ORIGIN = [f"machine:{deny.ME}"]


class PoolRefusingHub(deny.RecordingHub):
    """Refuses the self-check's pool repin (403); the origin repin is refused only when ``refuse_origin``."""

    def __init__(self, *, refuse_origin=False):
        super().__init__()
        self.refuse_origin = refuse_origin

    def repin(self, board, **body):
        self.refuse_repin = "forbidden" if body["runs_on"] != ORIGIN or self.refuse_origin else None
        return super().repin(board, **body)


def _gate_refused(conn):
    """A stranded card (36 min, no runs_on) whose wire row has a malformed ``requires``: the self-check still targets
    the pool (``hub:write`` -> ``capability:hub-write``), but every pickup gate refuses it as invalid."""
    tid = deny._stranded(conn, age=36 * 60)
    conn.execute("UPDATE kanban_sync_state SET applying=1")
    conn.execute("UPDATE tasks SET requires=? WHERE id=?", (json.dumps(["hub:write", 1]), tid))
    conn.execute("UPDATE kanban_sync_state SET applying=0")
    conn.commit()
    row = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert coord.claim_refusal(row, deny.WORKER["name"]) == coord.INVALID_REQUIRES
    return tid


def _mismatch_comments(conn, tid):
    """``(capability_mismatch events, pickup-gate comments)``. Each Board refusal also notes "Board observation
    failed"; only the gate's comment must stay single."""
    events = [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, queue.EVENT))]
    comments = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? AND author='worker-placement'", (tid,)).fetchall()
    return (len([e for e in events if e.get("code") == "capability_mismatch"]),
            len([c for (c,) in comments if c.startswith("No pool worker can claim this card")]))


def test_a_refused_pool_repin_still_sends_a_gate_refused_card_to_its_origin(hub, no_pull):
    _home, conn = hub
    tid = _gate_refused(conn)
    board = PoolRefusingHub()
    for _ in range(2):
        wp.reevaluate(conn, board, deny.BOARD, deny.ME, cfg=deny.CFG)
    assert board.repins[0]["runs_on"][0] == "pool:worker"
    assert [r["runs_on"] for r in board.repins[1:]] == [ORIGIN], "the origin repin was never asked at revision 4"
    assert all(r["expected_revision"] == 4 for r in board.repins)
    assert board.reserves == []
    assert _mismatch_comments(conn, tid) == (1, 1)


def test_a_refused_origin_repin_is_not_asked_again_at_that_revision(hub, no_pull):
    _home, conn = hub
    tid = _gate_refused(conn)
    board = PoolRefusingHub(refuse_origin=True)
    for _ in range(4):
        wp.reevaluate(conn, board, deny.BOARD, deny.ME, cfg=deny.CFG)
    assert [r["runs_on"] == ORIGIN for r in board.repins] == [False, True]
    assert board.reserves == [] and _mismatch_comments(conn, tid) == (1, 1)
    # A new revision asks the pool repin again, then the origin repin once more.
    deny._bump(conn, tid, 5)
    for _ in range(3):
        wp.reevaluate(conn, board, deny.BOARD, deny.ME, cfg=deny.CFG)
    assert [(r["runs_on"] == ORIGIN, r["expected_revision"]) for r in board.repins[2:]] == [(False, 5), (True, 5)]
    assert board.reserves == []
