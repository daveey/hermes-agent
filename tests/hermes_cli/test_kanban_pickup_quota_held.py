"""Pickup spends no budget on a card the local dispatcher would hold on its quota episode.

worker-3 2026-11-12 (t_01a1cf9f2b838cbb): four foreign cards walled until 11-17/18 (``subscription_quota_reset``)
filled every pickup batch. Each tick copied them, local dispatch held them, the next tick evicted and copied them
again (126 pickups in 26 min), and two priority-0 cards pinned to the machine waited ``budget_exhausted``.
"""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_pickup_stall as kps
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as support
from tests.hermes_cli.test_kanban_pickup_stall import alarm_worker_pool

svc = support.svc
gws = support.gws
PIN = [f"machine:{support.WORKER}"]
WALL_S = 5 * 86400


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


def _walled(hub, *, title, priority, runs_on, providers=()):
    """A hub card whose latest run hit a quota wall with a typed reset days away (a real episode, synced)."""
    hub.enter()
    tid = kb.create_task(hub.conn, title=title, assignee="default", priority=priority, runs_on=runs_on)
    now = int(time.time())
    with kb.write_txn(hub.conn):
        run = hub.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
            "VALUES (?, 'default', 'released', ?, ?, 'rate_limited')", (tid, now - 120, now - 60)).lastrowid
        kb._append_event(hub.conn, tid, "quota_episode", {
            "episode_id": run, "run_id": run, "count": 1, "until": now + WALL_S, "hinted": True,
            "provider": providers[0] if providers else None, "providers": list(providers), "model": None},
            run_id=run)
    hub.round()
    return tid


def _status(conn, tid):
    t = kb.get_task(conn, tid)
    return t.status if t else None


def test_quota_held_cards_do_not_starve_a_pinned_card(gws, imports):
    hub, worker = gws
    walled = [_walled(hub, title=f"walled {i}", priority=1000 - i, runs_on=["linux"] if i % 2 else PIN)
              for i in range(3)]
    pinned = support._hub_card(hub, title="pinned, eligible", priority=0, runs_on=PIN)
    worker.round()
    worker.enter()
    # Parity: on a copy, the local dispatcher's own guard holds each walled card on that wall.
    replica = wn.open_replica(worker.conn)
    assert replica is not None
    try:
        assert wn.import_cards(worker.conn, replica, walled[:1]) == walled[:1]
    finally:
        replica.close()
    assert kbd.check_respawn_guard(worker.conn, walled[0]) == "subscription_quota_reset"
    assert wn.evict(worker.conn) == walled[:1]
    imports.clear()
    # Budget 2 < 3 walled cards ahead of the pinned one: the first tick copies and starts the pinned card.
    worker.tick(max_spawn=2)
    assert imports == [pinned]
    assert _status(worker.conn, pinned) == "running"
    for _ in range(3):  # and no later tick copies a walled card: no import/evict churn
        worker.round()
        worker.tick(max_spawn=3)
    assert imports == [pinned]
    assert all(_status(worker.conn, tid) is None for tid in walled)
    # A walled card pinned here says why it waits, not budget_exhausted.
    skip = kps.latest(worker.conn, walled[0])
    assert skip is not None and skip["reason"] == "quota_hold"
    assert skip["detail"].startswith("subscription_quota_reset until ")


def test_a_quota_held_pinned_card_files_no_repair_until_its_deadline_passes(gws):
    """Review 5874270282: recorded as the machine-wide ``hold``, a walled pinned card filed "Pickup held on <host>
    ... do not lift it" after 600 s while the machine was not held. Its own wall is expected until its deadline."""
    from tests.hermes_cli.test_kanban_pickup_stall import _age, _repairs
    hub, worker = gws
    tid = _walled(hub, title="walled, pinned", priority=10, runs_on=PIN)
    worker.round()
    worker.tick(max_spawn=1)
    assert _status(worker.conn, tid) is None
    _age(worker, tid, 3600)
    t0 = time.time()
    assert kps.sweep(worker.conn, now=t0) == []
    assert kps.sweep(worker.conn, now=t0 + WALL_S + kps.stall_seconds() - 120) == []
    assert not _repairs(worker.conn)
    # The wall outlived its deadline and the card is still not here: a card-specific repair, never the host hold.
    kps.sweep(worker.conn, now=t0 + WALL_S + kps.stall_seconds() + 120)
    [repair] = _repairs(worker.conn)
    assert tid in repair["title"] and "quota_hold" in repair["title"]
    assert "held on" not in repair["title"].lower()


def test_an_episode_whose_wall_has_passed_is_copied(gws, imports, monkeypatch):
    hub, worker = gws
    tid = _walled(hub, title="wall over", priority=10, runs_on=PIN)
    worker.round()
    worker.enter()
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + WALL_S + 600)
    assert wn.pickup(worker.conn, budget=1) == [tid]


def test_a_failed_hold_check_copies_as_before(gws, imports, monkeypatch):
    hub, worker = gws
    tid = _walled(hub, title="walled", priority=10, runs_on=PIN)
    worker.round()
    worker.enter()
    from hermes_cli import kanban_quota_episode as qe

    def boom(*a, **kw):
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(qe, "decision", boom)
    assert wn.pickup(worker.conn, budget=1) == [tid]
