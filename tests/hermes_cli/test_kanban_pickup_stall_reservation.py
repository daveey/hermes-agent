"""A worker-first card the Board reserves to this machine only briefly is not a pinned-card stall here, and one card
on one machine gets at most one open host-wide repair.

t_01a1cfdfb6f3f7ea (2026-11-12): MacBook card t_01a1cfa91548dad7 (``worker-first-v1``, ready since 12:09Z) was
reserved to worker-2 for seconds at a time (12:40:32-12:40:43Z, 12:49:27-12:50:46Z; ``runs_on`` null in between,
recorded only as the origin's ``worker_queue_reason`` observations, never as a placement event). worker-2 counted
from ``ready_since`` and filed "Pickup evidence missing ... waiting 40 min" at 12:50:44Z; pickup recorded
budget_exhausted 0.3 s later, and the next tick filed "Pickup queue full ... waiting 40 min" for the same card.
Same fake-Board harness as ``test_kanban_pickup_stall``.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import kanban_pickup_stall as kps
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_pickup_stall as base

WORKER = base.WORKER
gws = base.gws
svc = base.svc
alarm_worker_pool = base.alarm_worker_pool


def _observed(worker, tid, ago, runs_on):
    """The origin's worker-placement observation ``ago`` seconds back, synced into the worker's replica:
    ``lifecycle.runs_on`` is the origin's ``runs_on`` JSON text (None: unpinned)."""
    payload = {"version": 1, "code": "reserved", "state": "assigned", "machine": WORKER,
               "lifecycle": {"runs_on": None if runs_on is None else json.dumps(runs_on), "run_count": 0}}
    with sqlite3.connect(wn._replica_path(worker.conn)) as rw:
        rw.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'worker_queue_reason', ?, "
                   "CAST(strftime('%s','now') AS INTEGER) - ?)", (tid, json.dumps(payload), ago))


def _reserved_card(hub, worker):
    tid = base._pinned(hub, title="reserved by the Board")  # the Board row says machine:<worker> right now
    worker.round()
    base._age(worker, tid, 2400)  # ready 40 min, like t_01a1cfa91548dad7
    return tid


def test_a_brief_reservation_of_a_long_ready_card_raises_no_alarm(gws):
    hub, worker = gws
    tid = _reserved_card(hub, worker)
    _observed(worker, tid, 1800, None)
    _observed(worker, tid, 600, [f"machine:{WORKER}"])  # an earlier reservation...
    _observed(worker, tid, 590, None)                   # ...that lapsed
    _observed(worker, tid, 9, None)                     # reserved again 9 s ago (the Board answered 'assigned')
    _observed(worker, tid, 7, [f"machine:{WORKER}"])
    assert not base._queue_full_repairs(worker)


def test_a_reservation_held_past_the_interval_still_raises_its_alarm_from_the_reservation(gws):
    hub, worker = gws
    tid = _reserved_card(hub, worker)
    _observed(worker, tid, 700, None)                   # last seen unpinned 11 min ago, pinned here since
    _observed(worker, tid, 690, [f"machine:{WORKER}"])
    _observed(worker, tid, 30, [f"machine:{WORKER}"])
    [card] = base._queue_full_repairs(worker)
    assert tid in card["title"] and "waiting 11 min" in card["title"]


def test_observations_naming_this_machine_or_bad_payloads_do_not_restart_the_clock(gws):
    hub, worker = gws
    tid = _reserved_card(hub, worker)
    _observed(worker, tid, 30, [f"machine:{WORKER}"])
    with sqlite3.connect(wn._replica_path(worker.conn)) as rw:
        rw.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'worker_queue_reason', "
                   "'not json', CAST(strftime('%s','now') AS INTEGER))", (tid,))
    replica = wn.open_replica(worker.conn)
    try:
        [card] = kps.stalled(worker.conn, replica, WORKER)
    finally:
        replica.close()
    assert card["id"] == tid and kps._now_ms() - card["since_ms"] >= 2390_000


def test_a_card_whose_host_wide_cause_changes_keeps_one_open_repair(gws, monkeypatch):
    hub, worker = gws
    tid = base._pinned(hub, title="evidence then queue full")
    worker.round()
    base._age(worker, tid, 2400)
    clock = [kps._now_ms() - 601_000]
    monkeypatch.setattr(kps, "_now_ms", lambda now=None: clock[0])
    # The detector's own row from an earlier tick, one stall interval ago.
    kps.record(worker.conn, {tid: ("evidence_missing", "detector observation; no pickup skip recorded")})
    clock[0] += 601_000
    worker.tick(max_spawn=0)  # sweep files "Pickup evidence missing"; the capped path then records budget_exhausted
    [first] = base._repairs(worker.conn)
    assert "evidence" in first["title"].lower()
    assert kps.latest(worker.conn, tid)["reason"] == "budget_exhausted"
    worker.tick(max_spawn=0)  # same card, now budget_exhausted: no "Pickup queue full" beside it
    worker.tick(max_spawn=0)
    assert [r["id"] for r in base._repairs(worker.conn)] == [first["id"]]


@pytest.mark.parametrize("status", ["done", "archived"])
def test_a_closed_host_wide_repair_does_not_cover_another_cause(gws, status):
    from hermes_cli import kanban_db as kb
    _, worker = gws
    worker.enter()
    card = {"id": "t_0000target", "title": "waiting", "tenant": None, "since_ms": 1,
            "skip": {"reason": "evidence_missing", "first_at": 1, "last_at": 1, "passes": 1, "detail": None}}
    [evidence] = kps.file_repairs(worker.conn, WORKER, [card])
    with kb.write_txn(worker.conn):
        worker.conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, evidence))
    full = {**card, "skip": {**card["skip"], "reason": "budget_exhausted"}}
    [queue] = kps.file_repairs(worker.conn, WORKER, [full])
    assert queue != evidence and "queue" in kb.get_task(worker.conn, queue).title.lower()
