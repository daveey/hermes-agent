"""A home-owned spend decision must fence claims on a foreign executor too. [daveey]"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_spend_budget as spend_helpers
from tests.hermes_cli import test_kanban_worker_node as helpers

kanban_home = spend_helpers.kanban_home

svc = helpers.svc
gws = helpers.gws


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_reclaimed_copy_cannot_claim_while_home_reholds(gws, svc, monkeypatch, lane):
    """Real pickup/sync, stale ready copy, home hold not yet published: no new lease/run."""
    hub, worker = gws
    hub.enter()
    tid = kb.create_task(hub.conn, title="Previously stopped spending", assignee="default", runs_on=["linux"])
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET status=? WHERE id=?", (lane, tid))
    claim = kb.claim_task if lane == "ready" else kb.claim_review_task
    assert claim(hub.conn, tid) is not None
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, tid, decision.EVENT, {"reason": "budget already spent"})
    assert kb.reclaim_task(hub.conn, tid, reason="scratch updater requeue", signal_fn=lambda *a: None)
    assert kb.get_task(hub.conn, tid).status == lane
    hub.round()
    worker.round()
    worker.enter()
    assert wn.pickup(worker.conn, budget=1) == [tid]
    assert decision.pending(worker.conn, tid)
    assert worker.conn.execute("SELECT created_on FROM tasks WHERE id=?", (tid,)).fetchone()[0] == helpers.HUB
    before = len(helpers._runs(worker.conn, tid))
    hub.enter()
    monkeypatch.setattr(decision, "safe_ensure_producer", lambda *a: None)
    assert budget.hold(hub.conn, tid) == "triage"
    # Deliberately do NOT sync the status hold before the foreign claim.
    worker.enter()
    assert kb.get_task(worker.conn, tid).status == lane
    claim = kb.claim_task if lane == "ready" else kb.claim_review_task
    assert claim(worker.conn, tid) is None
    assert len(helpers._runs(worker.conn, tid)) == before
    assert svc.board(helpers.BOARD)["rows"]["tasks"][tid]["status"] == lane
    worker.round()
    hub.round()
    worker.round()
    assert kb.get_task(worker.conn, tid).status == "triage"
    assert not worker.conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='claimed' "
                                   "AND origin_gateway=?", (tid, helpers.WORKER)).fetchall()
    hub.enter()
    assert kb.promote_task(hub.conn, tid, actor="operator", reason="bounded new scope")[0]
    hub.round()
    worker.round()
    worker.enter()
    assert not decision.pending(worker.conn, tid)
    assert kb.claim_task(worker.conn, tid) is not None


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_first_budget_crossing_on_foreign_copy_refuses_without_home_stop(gws, lane):
    hub, worker = gws
    hub.enter()
    tid = kb.create_task(hub.conn, title="First spend crossing", assignee="default", runs_on=["linux"])
    spend_helpers._runs(hub.conn, tid, budget.DEFAULT_RUNS)
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET status=? WHERE id=?", (lane, tid))
    hub.round()
    worker.round()
    worker.enter()
    assert wn.pickup(worker.conn, budget=1) == [tid]
    assert not decision.pending(worker.conn, tid)
    assert budget.spend(worker.conn, tid).runs == budget.DEFAULT_RUNS
    claim = kb.claim_task if lane == "ready" else kb.claim_review_task
    before = len(helpers._runs(worker.conn, tid))
    assert claim(worker.conn, tid) is None
    assert len(helpers._runs(worker.conn, tid)) == before
    assert not decision.pending(worker.conn, tid)  # only home may produce the decision


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_stop_arriving_during_lease_acquisition_refuses_claim(kanban_home, monkeypatch, lane):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_sync as sync

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Stop arrived during admission", assignee="default")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (lane, tid))
        real = sync.claim_gate
        entered = []

        def arriving(c, task_id, expires, **kw):
            gate = real(c, task_id, expires, **kw)
            with kb.write_txn(c):
                kb._append_event(c, task_id, decision.EVENT, {"reason": "concurrent stop"})
            entered.append(task_id)
            return gate

        monkeypatch.setattr(sync, "claim_gate", arriving)
        claim = kb.claim_task if lane == "ready" else kb.claim_review_task
        assert claim(conn, tid) is None
        assert entered == [tid]
        assert not conn.execute("SELECT id FROM task_runs WHERE task_id=?", (tid,)).fetchall()
        assert kb.get_task(conn, tid).status == lane


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_pending_stop_refuses_before_any_lease(kanban_home, monkeypatch, lane):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_sync as sync

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Unreleased stop", assignee="default")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, decision.EVENT, {"reason": "spent"})
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (lane, tid))

        def forbidden(*a, **kw):
            pytest.fail("pending spend decision must not acquire a Board lease")

        monkeypatch.setattr(sync, "claim_gate", forbidden)
        claim = kb.claim_task if lane == "ready" else kb.claim_review_task
        assert claim(conn, tid) is None
        assert not conn.execute("SELECT id FROM task_runs WHERE task_id=?", (tid,)).fetchall()
