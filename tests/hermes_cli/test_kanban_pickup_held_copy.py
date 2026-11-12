"""A copy the local dispatcher holds (respawn guard) stays, so pickup never re-spends its budget on it.

worker-3 2026-11-12: at 9/10 in progress (pickup budget 1) an ``active_pr``-held card was evicted and
re-imported every tick and took the only slot; two cards pinned to the machine waited ``budget_exhausted``."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as support

svc = support.svc
gws = support.gws
PIN = [f"machine:{support.WORKER}"]


@pytest.fixture
def guard(monkeypatch):
    """``check_respawn_guard`` holding the ids in ``held`` (the real guard needs a PR comment plus gh state)."""
    held: dict[str, str] = {}
    real = kbd.check_respawn_guard
    monkeypatch.setattr(kbd, "check_respawn_guard",
                        lambda conn, tid, lane="ready": held.get(tid) or real(conn, tid, lane=lane))
    return held


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


def _status(conn, tid):
    t = kb.get_task(conn, tid)
    return t.status if t else None


def test_a_held_copy_does_not_take_the_pickup_budget_every_tick(gws, guard, imports):
    hub, worker = gws
    held = support._hub_card(hub, title="held by active_pr", priority=900, runs_on=PIN)
    pinned = support._hub_card(hub, title="pinned, eligible", priority=0, runs_on=PIN)
    guard[held] = "active_pr"
    for _ in range(4):
        worker.round()
        worker.tick(max_spawn=1)  # budget 1: nothing runs yet
        if _status(worker.conn, pinned) == "running":
            break
    assert _status(worker.conn, pinned) == "running"
    assert imports.count(pinned) == 1
    assert imports.count(held) == 1  # copied once, then kept while held
    assert _status(worker.conn, held) == "ready"
    for _ in range(2):  # the held copy is never copied again while the guard holds it
        worker.round()
        worker.tick(max_spawn=2)
    assert imports.count(held) == 1 and _status(worker.conn, held) == "ready"


def test_a_copy_whose_guard_lifts_is_evicted(gws, guard):
    hub, worker = gws
    tid = support._hub_card(hub, runs_on=PIN)
    guard[tid] = "active_pr"
    worker.round()
    worker.tick()
    worker.round()  # push the recorded hold
    worker.enter()
    assert wn.evict(worker.conn) == [] and _status(worker.conn, tid) == "ready"
    guard.pop(tid)
    assert wn.evict(worker.conn) == [tid]


def test_a_copy_the_dispatcher_never_held_is_evicted(gws, guard):
    hub, worker = gws
    tid = support._hub_card(hub, runs_on=PIN)
    guard[tid] = "active_pr"
    worker.round()
    worker.enter()
    replica = wn.open_replica(worker.conn)
    try:
        assert wn.import_cards(worker.conn, replica, [tid]) == [tid]
    finally:
        replica.close()
    assert wn.evict(worker.conn) == [tid]  # no recorded hold here: the guard alone does not keep it


def test_a_held_copy_that_leaves_the_lane_is_evicted(gws, guard):
    hub, worker = gws
    tid = support._hub_card(hub, runs_on=PIN)
    guard[tid] = "active_pr"
    worker.round()
    worker.tick()
    worker.round()
    worker.enter()
    assert wn.evict(worker.conn) == []
    worker.conn.execute("UPDATE kanban_sync_state SET applying = 1")
    worker.conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    worker.conn.execute("UPDATE kanban_sync_state SET applying = 0")
    worker.conn.commit()
    assert wn.evict(worker.conn) == [tid]


def test_a_failed_guard_check_evicts_as_before(gws, guard, monkeypatch):
    hub, worker = gws
    tid = support._hub_card(hub, runs_on=PIN)
    guard[tid] = "active_pr"
    worker.round()
    worker.tick()
    worker.round()
    worker.enter()

    def boom(conn, task_id, lane="ready"):
        raise RuntimeError("gh down")
    monkeypatch.setattr(kbd, "check_respawn_guard", boom)
    assert wn.evict(worker.conn) == [tid]
