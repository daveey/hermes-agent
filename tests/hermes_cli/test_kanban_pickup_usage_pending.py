"""Pickup spends no budget on a card whose claim would defer on ``usage_accounting_pending``.

2026-11-26 (t_01a2159cd5eccf77): 30 of 51 pool candidates had a closed run without a usage receipt (pre-turn
429s). Every tick copied the oldest of them, the local claim deferred them (``kanban_spend_claim``), and the
batch never reached the design review of the accounting fix itself, ready at priority 900.
"""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_usage_accounting as ua
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as support

svc = support.svc
gws = support.gws
PIN = [f"machine:{support.WORKER}"]


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


def _indebted(hub, *, title, priority, runs_on):
    """A hub card whose bound run closed without any usage receipt (a real accounting debt, synced)."""
    hub.enter()
    tid = kb.create_task(hub.conn, title=title, assignee="default", priority=priority, runs_on=runs_on)
    now = int(time.time())
    with kb.write_txn(hub.conn):
        run = hub.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
            "VALUES (?, 'default', 'released', ?, ?, 'released')", (tid, now - 120, now - 60)).lastrowid
        kb._append_event(hub.conn, tid, "worker_bound", {"pid": 1}, run_id=run)
        kb._append_event(hub.conn, tid, "worker_result", {"exit_code": 0},
                         run_id=run)
    assert ua.pending(hub.conn, tid) == [run]
    hub.round()
    return tid


def test_usage_pending_cards_do_not_starve_a_clean_card(gws, imports):
    hub, worker = gws
    indebted = [_indebted(hub, title=f"debt {i}", priority=1000 - i, runs_on=PIN) for i in range(3)]
    clean = support._hub_card(hub, title="clean, eligible", priority=0, runs_on=PIN)
    worker.round()
    worker.enter()
    # Budget 2 < 3 indebted cards ahead of the clean one: the first pass copies the clean card only.
    assert wn.pickup(worker.conn, budget=2) == [clean]
    for _ in range(2):
        worker.round()
        worker.enter()
        wn.pickup(worker.conn, budget=3)
    assert set(imports) == {clean}  # an unstarted copy is evicted and copied again; never a debt card
    assert all(kb.get_task(worker.conn, tid) is None for tid in indebted)


def test_a_failed_accounting_check_does_not_refuse(gws, monkeypatch):
    """A failed read refuses nothing at pickup: the claim check still decides after the copy."""
    hub, worker = gws
    tid = _indebted(hub, title="debt", priority=10, runs_on=PIN)
    worker.round()
    worker.enter()
    replica = wn.open_replica(worker.conn)
    assert replica is not None
    try:
        card = replica.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert wn._usage_pending(replica, card)[1].startswith("usage_accounting_pending: runs ")

        def boom(*a, **kw):
            raise RuntimeError("replica unreadable")
        monkeypatch.setattr(ua, "pending", boom)
        assert wn._usage_pending(replica, card) is None
    finally:
        replica.close()
