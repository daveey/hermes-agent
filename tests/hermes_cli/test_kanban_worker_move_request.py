"""A worker-first card that a pool worker moves away (``kanban_move``) is repinned by its origin, never re-reserved.

Seen 2026-11-03 on t_01a1a13d757b23af: the hub created a ``worker-first-v1`` card, the Board reserved it for
worker-1, and worker-1's run moved it to ``machine:hub`` twice. A policy card's ``runs_on`` is Board-owned (the
outbox strips it, the Board fences a field edit), so each move changed nothing on the Board; the hub reserved the
card for worker-1 again and then hit ``409 not_claimable`` on the next tick, in a loop. Entry paths covered: the core
``kanban_move.move_task`` run on the worker, and the origin's placement tick ``kanban_worker_placement.reevaluate``.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_move as km
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_worker_node as node_fixtures
from tests.hermes_cli import test_kanban_worker_policy_db as db_fixtures
from tests.hermes_cli.test_kanban_worker_node import BOARD, HUB, WORKER
from tests.hermes_cli.test_kanban_worker_repin import RepinHub

# kanban_move_pool: a machine:<x> target needs a host_reason naming state on x (t_01a1d95c41620e6c).
HR = "the card's worktree is on that host"

conn = db_fixtures.conn
gws = node_fixtures.gws
svc = node_fixtures.svc
POOLS = {"worker_pools": {HUB: [WORKER]}}


class PlacementHub(RepinHub):
    """RepinHub plus the Board's ``reserveWorker`` happy path: it assigns the card to worker-1 again."""

    def __init__(self, svc_, gateway):
        super().__init__(svc_, gateway)
        self.reserves = []

    def reserve(self, board, *, task_id, expected_revision, candidates):
        self._check()
        self.reserves.append(task_id)
        b = self.svc.board(board)
        self.svc._put(b, "tasks", task_id, {"runs_on": json.dumps([f"machine:{candidates[0]}"])},
                      self.gateway, None, f"{9_999_999_999_996:013d}-0000-board")
        return {"state": "assigned", "machine": candidates[0], "reservation_id": "r", "expires_at": 1,
                "reason": "worker_capacity_reserved"}


def _moved(conn_, tid):
    return [json.loads(p or "{}") for (p,) in conn_.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, km.MOVED_EVENT))]


def _worker_runs_policy_card(origin, worker, svc_):
    """The origin creates a policy card; the Board assigns it to worker-1, which claims and runs it."""
    origin.enter()
    tid = kb.create_task(origin.conn, title="askeve fix", assignee="default", placement_policy=wp.POLICY)
    origin.round()
    origin.round()
    b = svc_.board(BOARD)
    svc_._put(b, "tasks", tid, {"runs_on": json.dumps([f"machine:{WORKER}"])}, HUB, None,
              f"{9_999_999_999_990:013d}-0000-board")
    worker.round()
    worker.tick()
    assert b["leases"][tid]["holder"] == WORKER
    worker.round()
    run_id = worker.conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    return tid, run_id


def test_a_worker_move_back_to_the_origin_is_repinned_there_and_the_origin_runs_it(gws, svc, monkeypatch):
    origin, worker = gws
    monkeypatch.setattr(wp, "local_admission", lambda conn_: True)  # worker-1 has capacity (host metrics)
    monkeypatch.setitem(node_fixtures.MACHINES[HUB], "pickup", "explicit")  # as the live registry says (11-03)
    tid, run_id = _worker_runs_policy_card(origin, worker, svc)

    worker.enter()
    res = km.move_task(worker.conn, tid, runs_on=[f"machine:{HUB}"], host_reason=HR, reason="needs the hub's askeve runtime",
                       checkpoint="draft f9f05dd6e; next: fix the red suites", expected_run_id=run_id, force=True)
    assert res["ok"] and res["status"] == "ready", res
    assert res.get("repin") == "requested", res  # the worker cannot write a Board-owned pin; the origin applies it
    [ev] = _moved(worker.conn, tid)
    assert ev["runs_on"] == [f"machine:{HUB}"] and ev.get("repin_requested") is True
    worker.round()

    origin.round()
    assert kb.get_task(origin.conn, tid).status == "ready"
    hub = PlacementHub(svc, HUB)
    origin.enter()
    wp.reevaluate(origin.conn, hub, BOARD, HUB, cfg=POOLS)
    assert hub.reserves == []  # never reserved for worker-1 again
    assert [r["runs_on"] for r in hub.repins] == [[f"machine:{HUB}"]]
    row = origin.conn.execute("SELECT placement_policy, runs_on FROM tasks WHERE id=?", (tid,)).fetchone()
    assert (row["placement_policy"], json.loads(row["runs_on"])) == (None, [f"machine:{HUB}"])
    assert _moved(origin.conn, tid)[-1].get("repinned") is True

    # A second tick has nothing left to do: the policy is gone, so the origin never reserves it again.
    wp.reevaluate(origin.conn, hub, BOARD, HUB, cfg=POOLS)
    assert hub.reserves == [] and len(hub.repins) == 1

    # The hub dispatcher claims it under a Board lease; worker-1 cannot.
    origin.tick()
    assert kb.get_task(origin.conn, tid).status == "running"
    assert svc.board(BOARD)["leases"][tid]["holder"] == HUB
    worker.round()
    worker.tick()
    assert svc.board(BOARD)["leases"][tid]["holder"] == HUB


def test_a_reclaimed_move_of_a_policy_card_is_a_request_too(gws, svc, monkeypatch):
    origin, worker = gws
    monkeypatch.setattr(wp, "local_admission", lambda conn_: True)
    monkeypatch.setitem(node_fixtures.MACHINES[HUB], "pickup", "explicit")
    tid, _ = _worker_runs_policy_card(origin, worker, svc)
    # The fixture's spawner records this test process as the worker pid; reclaim must not SIGTERM it.
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda pid, lock, **kw: {
        "prev_pid": pid, "host_local": True, "termination_attempted": True, "terminated": True, "sigkill": False})
    worker.enter()
    before = worker.conn.execute("SELECT runs_on FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    res = km.move_reclaim(worker.conn, tid, runs_on=[f"machine:{HUB}"], host_reason=HR, reason="needs the hub", force=True)
    assert res["ok"] and res.get("repin") == "requested", res
    assert worker.conn.execute("SELECT runs_on FROM tasks WHERE id=?", (tid,)).fetchone()[0] == before
    assert _moved(worker.conn, tid)[-1].get("repin_requested") is True
    worker.round()
    origin.round()
    hub = PlacementHub(svc, HUB)
    origin.enter()
    wp.reevaluate(origin.conn, hub, BOARD, HUB, cfg=POOLS)
    assert hub.reserves == [] and [r["runs_on"] for r in hub.repins] == [[f"machine:{HUB}"]]


class RecordingHub:
    def __init__(self, *, refuse=None):
        self.reserves, self.repins, self.refuse = [], [], refuse

    def reserve(self, board, **body):
        self.reserves.append(body)
        if self.refuse:
            raise ksc.HubError(409, json.dumps({"error": self.refuse}))
        return {"state": "assigned", "machine": WORKER, "reservation_id": "r", "expires_at": 1,
                "reason": "worker_capacity_reserved"}

    def repin(self, board, **body):
        self.repins.append(body)
        return {"version": 9, "runs_on": body["runs_on"], "placement_policy": None}


def _acknowledged(conn_):
    tid = kb.create_task(conn_, title="Public analysis", assignee="default", placement_policy=wp.POLICY)
    conn_.execute("UPDATE kanban_sync_state SET applying=1")
    conn_.execute("UPDATE tasks SET sync_version=4, created_on='hub' WHERE id=?", (tid,))
    conn_.execute("UPDATE kanban_sync_state SET applying=0")
    conn_.execute("DELETE FROM kanban_sync_outbox")
    conn_.commit()
    return tid


@pytest.fixture
def no_pull(monkeypatch):
    monkeypatch.setattr(ksc, "pull", lambda *a, **kw: None)
    getattr(wp, "_refused", {}).clear()
    yield
    getattr(wp, "_refused", {}).clear()


def test_a_pulled_move_request_is_repinned_at_the_acknowledged_revision(conn, no_pull):
    tid = _acknowledged(conn)
    kb._append_event(conn, tid, km.MOVED_EVENT, {"from_machine": WORKER, "runs_on": ["machine:hub"],
                                                 "previous_runs_on": ["machine:worker-1"], "reason": "r",
                                                 "move_no": 1, "forced": False, "matches": ["hub"]})
    conn.commit()
    hub = RecordingHub()
    wp.reevaluate(conn, hub, "default", "hub", cfg=POOLS)
    assert hub.reserves == []
    assert hub.repins == [{"task_id": tid, "expected_revision": 4, "runs_on": ["machine:hub"]}]


def test_an_applied_repin_is_not_replayed(conn, no_pull):
    tid = _acknowledged(conn)
    kb._append_event(conn, tid, km.MOVED_EVENT, {"from_machine": WORKER, "runs_on": ["machine:hub"]})
    kb._append_event(conn, tid, km.MOVED_EVENT, {"from_machine": "hub", "runs_on": ["machine:hub"], "repinned": True})
    conn.commit()
    hub = RecordingHub()
    wp.reevaluate(conn, hub, "default", "hub", cfg=POOLS)
    assert hub.repins == [] and len(hub.reserves) == 1


def test_a_local_record_failure_after_a_board_repin_does_not_abort_the_batch(conn, no_pull, monkeypatch):
    first, second = _acknowledged(conn), _acknowledged(conn)
    for tid in (first, second):
        kb._append_event(conn, tid, km.MOVED_EVENT, {"from_machine": WORKER, "runs_on": ["machine:hub"]})
    conn.commit()

    def broken(*a, **kw):
        raise __import__("sqlite3").OperationalError("database is locked")
    monkeypatch.setattr(wp, "_record_repin", broken)
    hub = RecordingHub()
    wp.reevaluate(conn, hub, "default", "hub", cfg=POOLS)
    assert sorted(r["task_id"] for r in hub.repins) == sorted([first, second])


def test_not_claimable_backs_off_until_the_revision_changes(conn, no_pull):
    tid = _acknowledged(conn)
    hub = RecordingHub(refuse="not_claimable")
    for _ in range(3):
        wp.reevaluate(conn, hub, "default", "hub", cfg=POOLS)
    assert len(hub.reserves) == 1  # one refusal, not one per dispatcher tick
    conn.execute("UPDATE kanban_sync_state SET applying=1")
    conn.execute("UPDATE tasks SET sync_version=6 WHERE id=?", (tid,))
    conn.execute("UPDATE kanban_sync_state SET applying=0")
    conn.commit()
    wp.reevaluate(conn, hub, "default", "hub", cfg=POOLS)
    assert [r["expected_revision"] for r in hub.reserves] == [4, 6]


def test_not_claimable_backoff_expires(conn, no_pull, monkeypatch):
    _acknowledged(conn)
    hub = RecordingHub(refuse="not_claimable")
    clock = [1000.0]
    monkeypatch.setattr(wp.time, "monotonic", lambda: clock[0])
    wp.reevaluate(conn, hub, "default", "hub", cfg=POOLS)
    clock[0] += getattr(wp, "NOT_CLAIMABLE_BACKOFF_S", 600) + 1
    wp.reevaluate(conn, hub, "default", "hub", cfg=POOLS)
    assert len(hub.reserves) == 2


@pytest.mark.parametrize("code", ["revision_conflict", "unauthorized_candidate"])
def test_other_refusals_keep_retrying(conn, no_pull, code):
    _acknowledged(conn)
    hub = RecordingHub(refuse=code)
    wp.reevaluate(conn, hub, "default", "hub", cfg=POOLS)
    wp.reevaluate(conn, hub, "default", "hub", cfg=POOLS)
    assert len(hub.reserves) == 2
