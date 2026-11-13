"""A worker copy is never replaced by a replica older than what this gateway already pushed for it.

The sync round pulls the replica first and pushes the live outbox second, so rows this gateway just pushed (a
crashed PR owner's ``Opened <PR>`` comment, the ``reclaimed`` event) reach the replica only on the next round's
pull. A tick in between evicted the copy (outbox empty, no ``respawn_guarded`` recorded yet) and re-imported
it from the replica without those rows: ``active_pr`` did not hold and the PR owner was respawned with no
handoff (t_01a1cfedbfe95b4f)."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_replica_lag as lag
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import kanban_guard_helpers as guard_helpers
from tests.hermes_cli import test_kanban_worker_node as support

svc = support.svc
gws = support.gws
PIN = [f"machine:{support.WORKER}"]
PR = "https://github.com/daveey/hermes/pull/4242"


@pytest.fixture
def clock(monkeypatch):
    now = [float(int(time.time()))]
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    lag.reset()
    yield now
    lag.reset()


@pytest.fixture
def imports(monkeypatch):
    seen: list[str] = []
    real = wn.import_cards

    def counting(conn, replica, ids):
        done = real(conn, replica, ids)
        seen.extend(done)
        return done
    monkeypatch.setattr(wn, "import_cards", counting)
    return seen


def _crash_after_pr(worker, tid, clock):
    worker.round()
    worker.tick()
    assert kb.get_task(worker.conn, tid).status == "running"
    guard_helpers.worker_comment(worker.conn, tid, "default", f"Opened {PR}")
    clock[0] += 60
    assert kb.reclaim_task(worker.conn, tid, reason="worker crashed", signal_fn=lambda *_: None)
    assert kb.get_task(worker.conn, tid).status == "ready"
    assert kbd.check_respawn_guard(worker.conn, tid, lane="ready") == "active_pr"


def _comments(conn, tid):
    return conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)).fetchone()[0]


def test_a_crashed_pr_owner_stays_guarded_when_the_replica_lags_its_push(gws, clock, imports):
    hub, worker = gws
    tid = support._hub_card(hub, runs_on=PIN)
    _crash_after_pr(worker, tid, clock)
    worker.round()  # ONE round: the replica was pulled before this round pushed the comment and `reclaimed`
    clock[0] += 60
    worker.tick()
    task = kb.get_task(worker.conn, tid)
    assert task.status == "ready"  # not respawned
    assert _comments(worker.conn, tid) == 1  # the copy kept its PR comment
    assert imports.count(tid) == 1  # never replaced from the lagging replica
    assert kbd.check_respawn_guard(worker.conn, tid, lane="ready") == "active_pr"
    kinds = [r[0] for r in worker.conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]
    assert "respawn_guarded" in kinds and "reclaimed" in kinds
    # Once the replica has caught up the recorded hold keeps the copy (unchanged held-copy rule).
    worker.round()
    clock[0] += 60
    worker.tick()
    assert kb.get_task(worker.conn, tid).status == "ready" and imports.count(tid) == 1


def test_a_lagging_card_is_not_reimported_until_the_replica_catches_up(gws, clock, imports):
    """An evicted copy (guard not holding) is not copied back from a replica that lacks this gateway's push."""
    hub, worker = gws
    tid = support._hub_card(hub, runs_on=PIN)
    worker.round()
    worker.tick()
    assert kb.get_task(worker.conn, tid).status == "running"
    clock[0] += 60
    assert kb.reclaim_task(worker.conn, tid, reason="worker crashed", signal_fn=lambda *_: None)
    worker.round()  # pushes `reclaimed`; the replica pulled before it
    worker.enter()
    assert lag.lagging(wn._replica_path(worker.conn), [tid]) == {tid}
    assert wn.evict(worker.conn) == [tid]  # no hold: evicted as before
    assert wn.pickup(worker.conn, budget=4) == []  # ... but not re-imported from the lagging replica
    assert imports.count(tid) == 1
    worker.round()  # the replica now holds the push
    worker.enter()
    assert lag.lagging(wn._replica_path(worker.conn), [tid]) == set()
    assert wn.pickup(worker.conn, budget=4) == [tid]
    kinds = [r[0] for r in worker.conn.execute("SELECT kind FROM task_events WHERE task_id = ?", (tid,))]
    assert "reclaimed" in kinds  # the re-imported copy carries what this gateway pushed


@pytest.mark.parametrize("fields", [
    {"task_id": "t_1"},
    {"child_id": "t_1"},
    '{"task_id": "t_1"}',
    '{"child_id": "t_1"}',
])
def test_acked_task_ids_accepts_parsed_and_json_fields(fields):
    rows = [{"table_name": "task_events", "fields": fields}]
    assert lag.acked_task_ids(rows) == ["t_1"]


def test_a_lag_entry_expires(monkeypatch):
    lag.reset()
    mono = [1000.0]
    monkeypatch.setattr(lag, "_monotonic", lambda: mono[0])
    lag.acked("/r.db", ["t_1"])
    assert lag.lagging("/r.db", ["t_1", "t_2"]) == {"t_1"}
    mono[0] += lag.MAX_LAG_S + 1
    assert lag.lagging("/r.db", ["t_1"]) == set()  # a replica that never catches up does not pin a copy forever
    lag.acked("/r.db", ["t_1"])
    token = lag.pull_started("/r.db")
    lag.pull_done("/r.db", token)
    assert lag.lagging("/r.db", ["t_1"]) == set()
    lag.reset()
