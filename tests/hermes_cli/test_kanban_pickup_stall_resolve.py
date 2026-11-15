"""The guards of the pickup-stall resolve sweep (``hermes_cli.kanban_pickup_stall_resolve``). [daveey]

t_01a1d615bccc192f (2026-11-13): after #2439 each of these guards could be removed with the whole pickup-stall suite
still green. Each test below pins one: when a named card counts as having left ``ready`` here (leased on the Board,
re-pinned, imported here), which repairs the sweep leaves alone (leased on the Board, naming no card, claimed while
the sweep runs), an unreadable registry entry keeps a hold repair open, and a failing resolve never stops the
detector pass. Same fake-Board two-gateway harness as ``test_kanban_pickup_stall_lifted``.
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_pickup_stall as base
from tests.hermes_cli import test_kanban_pickup_stall_lifted as lifted
from tests.hermes_cli import test_kanban_worker_node as support

WORKER = support.WORKER
svc = support.svc
gws = support.gws
alarm_worker_pool = base.alarm_worker_pool
_HOLD = dict(hold_owner="ops@hub", hold_reason="disk migration")


def _held_repair(gws):
    """A hold repair naming two cards still ready and pinned here, under a hold that still stands.
    Returns (hub, worker, repair id, [a, b])."""
    from hermes_cli import kanban_pickup_stall as kps
    hub, worker, cards = lifted._held_cards(gws, hold_at=int(time.time() * 1000), **_HOLD)
    kps.sweep(worker.conn)
    [repair] = base._repairs(worker.conn)
    _still_held()
    return hub, worker, repair["id"], cards


def _still_held():
    lifted._registry(hold=True, hold_at=int(time.time() * 1000), **_HOLD)


def _replica_set(worker, tid, **cols):
    with sqlite3.connect(wn._replica_path(worker.conn)) as rw:
        sets = ", ".join(f"{k} = ?" for k in cols)
        n = rw.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*cols.values(), tid)).rowcount
    assert n == 1, tid


def _resolve(worker):
    from hermes_cli import kanban_pickup_stall_resolve as resolve
    replica = wn.open_replica(worker.conn)
    try:
        return resolve.sweep(worker.conn, replica, WORKER, now_ms=int(time.time() * 1000))
    finally:
        replica.close()


def _status(worker, rid):
    return kb.get_task(worker.conn, rid).status


def _note(worker, rid):
    [note] = [c.body for c in kb.list_comments(worker.conn, rid)
              if c.author == base.DETECTOR and "left ready" in c.body]
    return note


def test_a_hold_repair_whose_cards_still_wait_stays_open_while_held(gws):
    _, worker, rid, _ = _held_repair(gws)
    assert _resolve(worker) == []
    assert _status(worker, rid) == "ready"


def _lease(worker, tid, holder="worker-9"):
    _replica_set(worker, tid, lease_holder=holder, lease_expires=int(time.time() * 1000) + 600_000)


@pytest.mark.parametrize("how,state", [
    ("leased", "ready on the Board, leased by worker-9"),
    ("repinned", 'ready on the Board, re-pinned to ["machine:worker-9"]'),
    ("imported", "imported here (ready)"),
])
def test_a_card_leased_repinned_or_imported_here_has_left_ready(gws, how, state):
    """Each such card is no longer waiting on this host's pickup: once every named card is, the repair closes."""
    _, worker, rid, (a, b) = _held_repair(gws)
    for tid in (a, b):
        if how == "leased":
            _lease(worker, tid)
        elif how == "repinned":
            _replica_set(worker, tid, runs_on=json.dumps(["machine:worker-9"]))
    if how == "imported":
        replica = wn.open_replica(worker.conn)
        try:
            assert wn.import_cards(worker.conn, replica, [a, b]) == [a, b]
        finally:
            replica.close()
    _still_held()
    assert _resolve(worker) == [rid]
    assert _status(worker, rid) == "archived"
    note = _note(worker, rid)
    assert f"{a}: {state}" in note and f"{b}: {state}" in note


def test_an_expired_lease_still_counts_as_waiting(gws):
    _, worker, rid, (a, b) = _held_repair(gws)
    for tid in (a, b):
        _replica_set(worker, tid, lease_holder="worker-9", lease_expires=int(time.time() * 1000) - 1000)
    assert _resolve(worker) == []
    assert _status(worker, rid) == "ready"


@pytest.mark.parametrize("machines", [
    lambda: [m for m in support.seen_now(support.MACHINES.values()) if m.get("name") != WORKER],  # no entry
    lambda: None,  # registry unreadable
])
def test_a_missing_or_unreadable_registry_entry_keeps_a_hold_repair_open(gws, machines):
    """Fail toward keeping the alarm: only a readable, unheld entry for this machine counts as a lifted hold."""
    _, worker, rid, _ = _held_repair(gws)
    listed = machines()
    if listed is None:
        def unreadable():
            raise OSError("registry down")
        wn.set_registry_fetch(unreadable)
    else:
        wn.set_registry_fetch(lambda: {"me": WORKER, "machines": listed})
    reg = wn.registry()
    assert ((reg or {}).get("machines") or {}).get(WORKER) is None
    assert _resolve(worker) == []
    assert _status(worker, rid) == "ready"
    assert not [c for c in kb.list_comments(worker.conn, rid) if "lifted" in c.body]


def test_a_repair_leased_on_the_board_by_a_pool_run_is_left_to_it(gws):
    _, worker, rid, (a, b) = _held_repair(gws)
    worker.round()
    worker.round()  # push the repair, then pull the Board's copy into the replica
    for tid in (a, b):
        _lease(worker, tid)  # nothing is left for the repair to do ...
    _lease(worker, rid, holder="worker-7")  # ... but a pool run holds it on the Board
    _still_held()
    assert _resolve(worker) == []
    assert _status(worker, rid) == "ready"
    _replica_set(worker, rid, lease_expires=int(time.time() * 1000) - 1000)  # the lease lapses: it closes
    assert _resolve(worker) == [rid]


def test_a_repair_naming_no_card_is_left_to_its_run(gws):
    """Nothing named is not "every card left ready": with no target to judge, the sweep does not guess."""
    _, worker, rid, _cards = _held_repair(gws)
    with kb.write_txn(worker.conn):
        worker.conn.execute("UPDATE tasks SET body = 'hand-edited: no card list' WHERE id = ?", (rid,))
        worker.conn.execute("DELETE FROM task_comments WHERE task_id = ? AND author = ?", (rid, base.DETECTOR))
    from hermes_cli import kanban_pickup_stall_resolve as resolve
    assert [t for r, _, t in resolve.open_host_repairs(worker.conn, WORKER) if r == rid] == [[]]
    assert _resolve(worker) == []
    assert _status(worker, rid) == "ready"


def test_a_claim_landing_after_the_sweep_read_its_repair_is_honoured(gws, monkeypatch):
    """The archive re-checks the claim in its own transaction: a run claiming the repair between the sweep's read
    and the archive keeps it."""
    from hermes_cli import kanban_pickup_stall_resolve as resolve
    _, worker, rid, (a, b) = _held_repair(gws)
    for tid in (a, b):
        _lease(worker, tid)
    real = resolve._hold_lifted

    def claim_then_read(own):  # runs after the sweep's claim read, before _archive
        with kb.write_txn(worker.conn):
            worker.conn.execute("UPDATE tasks SET claim_lock = 'run-x' WHERE id = ?", (rid,))
        return real(own)

    monkeypatch.setattr(resolve, "_hold_lifted", claim_then_read)
    assert _resolve(worker) == []
    row = worker.conn.execute("SELECT status, claim_lock FROM tasks WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "ready" and row["claim_lock"] == "run-x"


def test_a_failing_resolve_never_stops_the_detector_pass(gws, monkeypatch):
    from hermes_cli import kanban_pickup_stall as kps
    from hermes_cli import kanban_pickup_stall_resolve as resolve
    _, worker, cards = lifted._held_cards(gws, hold_at=int(time.time() * 1000), **_HOLD)

    def broken(*a, **k):
        raise RuntimeError("resolve broke")

    monkeypatch.setattr(resolve, "sweep", broken)
    ids = kps.sweep(worker.conn)
    [repair] = base._repairs(worker.conn)
    assert ids == [repair["id"]] and cards[0] in repair["title"]
