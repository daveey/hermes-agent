"""A ``409 not_claimable`` that only means the reserved worker already claimed the card is not a refusal. [daveey]

Seen 2026-11-03 on probe t_01a19fd990770095 (hub): the Board reserved the card for worker-1 (05:21:31Z), worker-1
claimed it (05:21:51Z), and the hub's next placement call, made from a copy that had not pulled the lease yet, got
``409 not_claimable`` at 05:21:52Z. worker-placement recorded ``board_refused`` and commented "Board placement request
refused" although the card ran fine on worker-1. Entry path: the origin's placement tick
``kanban_worker_placement.reevaluate`` against the fake Board shared by the origin and the worker gateway.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_queue_reason as qr
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_worker_node as node_fixtures
from tests.hermes_cli import test_kanban_worker_policy_db as db_fixtures
from tests.hermes_cli.test_kanban_worker_move_request import PlacementHub, RecordingHub, _acknowledged
from tests.hermes_cli.test_kanban_worker_node import BOARD, HUB, WORKER
from tests.hermes_cli.test_kanban_worker_repin import _policy_card
from tests.hermes_cli import test_kanban_queue_reason_hold as hold

no_comment_hold = hold.no_comment_hold
conn = db_fixtures.conn
gws = node_fixtures.gws
svc = node_fixtures.svc
POOLS = {"worker_pools": {HUB: [WORKER]}}


class BoardHub(PlacementHub):
    """``reserveWorker`` as the Board runs it: ``not_claimable`` once the row is not ready or carries a lease."""

    def reserve(self, board, *, task_id, expected_revision, candidates):
        b = self.svc.board(board)
        task = b["rows"].get("tasks", {}).get(task_id) or {}
        if task.get("status") != "ready" or task_id in b["leases"]:
            self.reserves.append(task_id)
            raise ksc.HubError(409, json.dumps({"error": "not_claimable"}))
        return super().reserve(board, task_id=task_id, expected_revision=expected_revision, candidates=candidates)


@pytest.fixture(autouse=True)
def _fresh_backoff():
    wp._refused.clear()
    yield
    wp._refused.clear()


def _reasons(conn_, tid):
    return [json.loads(p)["code"] for (p,) in conn_.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, qr.EVENT))]


def _assigned_to(conn_, tid):
    latest = qr.latest(conn_, tid) or {}
    return latest.get("machine") if latest.get("state") == "assigned" else None


def _placement_comments(conn_, tid):
    return [c.body for c in kb.list_comments(conn_, tid) if c.author == "worker-placement"]


def test_not_claimable_after_the_reserved_worker_claimed_is_recorded_as_picked_up(gws, svc, monkeypatch):
    origin, worker = gws
    monkeypatch.setattr(wp, "local_admission", lambda conn_: True)  # worker-1 has capacity (host metrics)
    tid, _ = _policy_card(origin)
    hub = BoardHub(svc, HUB)
    origin.enter()
    assert wp.reevaluate(origin.conn, hub, BOARD, HUB, cfg=POOLS) == 1
    assert _assigned_to(origin.conn, tid) == WORKER

    # worker-1 pulls its assignment and claims the card on the Board; the origin has not pulled the lease yet.
    worker.round()
    worker.tick()
    assert svc.board(BOARD)["leases"][tid]["holder"] == WORKER
    origin.enter()
    row = origin.conn.execute("SELECT status, lease_holder FROM tasks WHERE id=?", (tid,)).fetchone()
    assert (row["status"], row["lease_holder"]) == ("ready", None)  # the stale copy the probe's hub had

    wp._refused.clear()
    wp.reevaluate(origin.conn, hub, BOARD, HUB, cfg=POOLS)
    assert len(hub.reserves) == 2  # the race really happened: the second reserve was refused not_claimable
    assert "board_refused" not in _reasons(origin.conn, tid)
    assert not any("refused" in body for body in _placement_comments(origin.conn, tid))
    row = origin.conn.execute("SELECT status, lease_holder FROM tasks WHERE id=?", (tid,)).fetchone()
    assert (row["status"], row["lease_holder"]) == ("running", WORKER)  # recorded as picked up by worker-1


@pytest.mark.usefixtures("no_comment_hold")
def test_not_claimable_without_a_claim_by_the_reserved_machine_is_still_refused(conn, monkeypatch):
    monkeypatch.setattr(ksc, "pull", lambda *a, **kw: None)  # the feed shows no lease: the row is still unclaimed
    tid = _acknowledged(conn)
    wp.reevaluate(conn, RecordingHub(), "default", "hub", cfg=POOLS)
    assert _assigned_to(conn, tid) == WORKER
    wp.reevaluate(conn, RecordingHub(refuse="not_claimable"), "default", "hub", cfg=POOLS)
    assert _reasons(conn, tid)[-1] == "board_refused" and len(_reasons(conn, tid)) == 2
    assert _placement_comments(conn, tid)[-1].startswith("Board placement request refused — HTTP 409")


def _pull_applying(conn_, tid, *, claims, lease_holder=None, holder="worker-2"):
    """A feed stub: after the reservation, it brings a Board claim (event, optionally the lease mirror)."""
    def pull(c, *a, **kw):
        if not claims:
            return
        c.execute("UPDATE kanban_sync_state SET applying=1")
        if lease_holder:
            c.execute("UPDATE tasks SET status='running', lease_holder=?, lease_expires=? WHERE id=?",
                      (lease_holder, 9_999_999_999_999, tid))
        c.execute("UPDATE kanban_sync_state SET applying=0")
        kb._append_event(c, tid, "claimed", {"holder": holder, "run_uid": "u", "expires": 1})
        c.commit()
    return pull


@pytest.mark.usefixtures("no_comment_hold")
def test_not_claimable_while_another_machine_holds_the_lease_is_still_refused(conn, monkeypatch):
    tid = _acknowledged(conn)
    claims = []
    monkeypatch.setattr(ksc, "pull", _pull_applying(conn, tid, claims=claims, lease_holder="worker-2"))
    wp.reevaluate(conn, RecordingHub(), "default", "hub", cfg=POOLS)
    assert _assigned_to(conn, tid) == WORKER
    claims.append(True)
    wp.reevaluate(conn, RecordingHub(refuse="not_claimable"), "default", "hub", cfg=POOLS)
    assert conn.execute("SELECT lease_holder FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "worker-2"
    assert _reasons(conn, tid)[-1] == "board_refused"  # recorded against the refused copy, as before the pull
    assert _placement_comments(conn, tid)[-1].startswith("Board placement request refused — HTTP 409")


def test_not_claimable_with_only_the_reserved_workers_claimed_event_is_a_pickup(conn, monkeypatch):
    tid = _acknowledged(conn)
    claims = []
    monkeypatch.setattr(ksc, "pull", _pull_applying(conn, tid, claims=claims, holder=WORKER))
    wp.reevaluate(conn, RecordingHub(), "default", "hub", cfg=POOLS)
    claims.append(True)
    wp.reevaluate(conn, RecordingHub(refuse="not_claimable"), "default", "hub", cfg=POOLS)
    assert "board_refused" not in _reasons(conn, tid)
    assert not any("refused" in body for body in _placement_comments(conn, tid))


def test_a_later_card_in_the_tick_is_judged_against_the_copy_its_reserve_was_made_from(conn, monkeypatch):
    """One pull per tick: cards after the first are refused or picked up by their pre-pull row and reservation."""
    first, leased_elsewhere, picked = _acknowledged(conn), _acknowledged(conn), _acknowledged(conn)
    monkeypatch.setattr(ksc, "pull", lambda *a, **kw: None)
    wp.reevaluate(conn, RecordingHub(), "default", "hub", cfg=POOLS)
    assert all(_assigned_to(conn, t) == WORKER for t in (first, leased_elsewhere, picked))

    def pull(c, *a, **kw):  # the tick's single pull brings both later cards' Board leases
        c.execute("UPDATE kanban_sync_state SET applying=1")
        for tid, holder in ((leased_elsewhere, "worker-2"), (picked, WORKER)):
            c.execute("UPDATE tasks SET status='running', lease_holder=?, lease_expires=? WHERE id=?",
                      (holder, 9_999_999_999_999, tid))
        c.execute("UPDATE kanban_sync_state SET applying=0")
        c.commit()
    monkeypatch.setattr(ksc, "pull", pull)
    hub = RecordingHub(refuse="not_claimable")
    wp.reevaluate(conn, hub, "default", "hub", cfg=POOLS)
    assert len(hub.reserves) == 3
    assert _reasons(conn, first)[-1] == "board_refused"
    assert _reasons(conn, leased_elsewhere)[-1] == "board_refused"
    assert "board_refused" not in _reasons(conn, picked)


def test_a_failing_pull_keeps_the_refusal_and_the_batch(conn, monkeypatch):
    first, second = _acknowledged(conn), _acknowledged(conn)
    monkeypatch.setattr(ksc, "pull", lambda *a, **kw: None)
    wp.reevaluate(conn, RecordingHub(), "default", "hub", cfg=POOLS)
    pulls = []

    def broken(*a, **kw):
        pulls.append(1)
        raise RuntimeError("schema ahead")
    monkeypatch.setattr(ksc, "pull", broken)
    hub = RecordingHub(refuse="not_claimable")
    wp.reevaluate(conn, hub, "default", "hub", cfg=POOLS)
    assert len(hub.reserves) == 2 and len(pulls) == 1  # one pull per tick, and the batch went on
    assert _reasons(conn, first)[-1] == _reasons(conn, second)[-1] == "board_refused"


def test_not_claimable_with_no_prior_reservation_is_refused_without_an_extra_pull(conn, monkeypatch):
    pulls = []
    monkeypatch.setattr(ksc, "pull", lambda *a, **kw: pulls.append(kw))
    tid = _acknowledged(conn)
    wp.reevaluate(conn, RecordingHub(refuse="not_claimable"), "default", "hub", cfg=POOLS)
    assert _reasons(conn, tid) == ["board_refused"] and pulls == []
