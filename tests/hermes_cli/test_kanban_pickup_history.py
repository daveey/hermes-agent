"""A picked-up card carries its whole event history, and a copy an older pickup truncated is refilled from the
replica (``kanban_worker_node._changes_for``, ``kanban_pickup_history``). [daveey]

Production 11-12: worker-1 re-picked-up the long hub card t_01a1ccf95e85a6f1 with only its newest 200 events, so its
early runs' ``owner_admitted`` rows were gone and ``hermes-core-wt ship`` refused to land PR #1204 (``run N started
an agent but recorded no session``). The land gate's rule is mirrored by :func:`_unadmitted_runs`."""

from __future__ import annotations

import argparse
import json
import sqlite3

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pickup_history as kph
from hermes_cli import kanban_sync_cli
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as fixtures

BOARD = fixtures.BOARD
gws, svc = fixtures.gws, fixtures.svc  # the shared hub + worker-1 gateways on one fake Board

LONG = 260  # more than the old 200-event pickup tail
EXECUTION = ("spawned", "owner_admitted", "worker_bound", "heartbeat", "completed")


def _unadmitted_runs(conn, tid):
    """Runs with execution events but no ``owner_admitted`` session: what ship's ``landing_sessions`` refuses."""
    events = kb.list_events(conn, tid)
    return sorted({e.run_id for e in events if e.kind in EXECUTION and e.run_id is not None}
                  - {e.run_id for e in events if e.kind == "owner_admitted" and (e.payload or {}).get("session_id")})


def _admissions(conn, tid):
    return {r["uid"]: r["session"] for r in conn.execute(
        "SELECT r.uid, json_extract(e.payload, '$.session_id') AS session FROM task_events e "
        "JOIN task_runs r ON r.id = e.run_id WHERE e.task_id = ? AND e.kind = 'owner_admitted'", (tid,))}


def _long_card_repicked_up(hub, worker):
    """A hub card run once on the worker (admitted, then LONG heartbeats), blocked, evicted, unblocked on the hub
    and picked up again by the worker: the production shape."""
    hub.enter()
    tid = kb.create_task(hub.conn, title="long card", assignee="default", runs_on=["linux"])
    hub.round()
    worker.round()
    worker.tick()
    worker.enter()
    run1 = worker.conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    assert run1 is not None
    with worker.conn:
        kb._append_event(worker.conn, tid, "owner_admitted", {"session_id": "sess-run1"}, run_id=run1)
        for _ in range(LONG):
            kb._append_event(worker.conn, tid, "heartbeat", None, run_id=run1)
    assert kb.block_task(worker.conn, tid, reason="needs a decision", expected_run_id=run1)
    worker.round()
    worker.tick()
    assert kb.get_task(worker.conn, tid) is None  # evicted once pushed
    hub.round()
    hub.enter()
    assert kb.unblock_task(hub.conn, tid)
    hub.round()
    worker.round()
    worker.tick()
    worker.enter()
    task = kb.get_task(worker.conn, tid)
    assert task is not None and task.status == "running" and task.current_run_id != run1
    with worker.conn:  # the new run's own admission, as a real spawn records it
        kb._append_event(worker.conn, tid, "owner_admitted", {"session_id": "sess-run2"}, run_id=task.current_run_id)
    return tid


def _replica(worker) -> sqlite3.Connection:
    worker.enter()
    replica = wn.open_replica(worker.conn)
    assert replica is not None
    return replica


def test_pickup_copies_the_whole_history_so_early_admissions_survive(isolate_usage_accounting, gws):
    hub, worker = gws
    tid = _long_card_repicked_up(hub, worker)
    replica = _replica(worker)
    try:
        want = {r[0] for r in replica.execute("SELECT uid FROM task_events WHERE task_id = ?", (tid,))}
        run1_uid = replica.execute("SELECT uid FROM task_runs WHERE task_id = ? ORDER BY id LIMIT 1",
                                   (tid,)).fetchone()[0]
    finally:
        replica.close()
    assert len(want) > LONG
    have = {r[0] for r in worker.conn.execute("SELECT uid FROM task_events WHERE task_id = ?", (tid,))}
    assert want <= have
    assert _admissions(worker.conn, tid)[run1_uid] == "sess-run1"
    assert worker.conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND id < 0", (tid,)).fetchone()[0] == 0
    # Copied oldest first: local ids follow the card's own order.
    created = [r[0] for r in worker.conn.execute("SELECT created_at FROM task_events WHERE task_id = ? ORDER BY id",
                                                 (tid,))]
    assert created == sorted(created)


def _truncate(worker, tid, keep):
    """What a pre-fix pickup left: only the newest ``keep`` events of the card (deleted without capture)."""
    conn = worker.conn
    with conn:
        conn.execute("UPDATE kanban_sync_state SET applying = 1")
        conn.execute("DELETE FROM task_events WHERE task_id = ? AND id NOT IN (SELECT id FROM task_events "
                     "WHERE task_id = ? ORDER BY id DESC LIMIT ?)", (tid, tid, keep))
        conn.execute("UPDATE kanban_sync_state SET applying = 0")


def test_refill_restores_a_truncated_running_copy_from_the_replica(isolate_usage_accounting, gws):
    hub, worker = gws
    tid = _long_card_repicked_up(hub, worker)
    conn = worker.conn
    _truncate(worker, tid, 200)
    run1 = conn.execute("SELECT id FROM task_runs WHERE task_id = ? ORDER BY id LIMIT 1", (tid,)).fetchone()[0]
    assert run1 in _unadmitted_runs(conn, tid)  # the land gate would refuse this copy
    before = {r["id"]: tuple(r) for r in conn.execute("SELECT * FROM task_events WHERE task_id = ?", (tid,))}
    top = conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0]
    outbox = conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0]
    replica = _replica(worker)
    try:
        held = {r[0] for r in conn.execute("SELECT uid FROM task_events WHERE task_id = ?", (tid,))}
        order = [r[0] for r in replica.execute("SELECT uid FROM task_events WHERE task_id = ? ORDER BY id", (tid,))
                 if r[0] not in held]
        missing = len(order)
        report = kph.refill(conn, replica, wn._own(conn))
        assert kph.refill(conn, replica, wn._own(conn)) == {}  # idempotent
    finally:
        replica.close()
    assert missing > 0 and report[tid]["filled"] == missing and report[tid]["unresolved"] == 0
    assert _unadmitted_runs(conn, tid) == []
    assert _admissions(conn, tid)
    # Existing rows untouched; refilled rows sort before them and never cross a notifier cursor (id > MAX).
    assert {r["id"]: tuple(r) for r in conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? AND id > 0", (tid,))} == before
    assert conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0] == top
    refilled = [r[0] for r in conn.execute("SELECT uid FROM task_events WHERE task_id = ? AND id < 0 ORDER BY id",
                                           (tid,))]
    assert refilled == order  # the card's own order, oldest lowest
    assert conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == outbox  # nothing pushed
    task = kb.get_task(conn, tid)
    assert task is not None and task.status == "running"


def test_refill_never_invents_a_missing_admission(isolate_usage_accounting, gws):
    """An admission the Board itself lacks stays missing, so the land gate still refuses."""
    hub, worker = gws
    tid = _long_card_repicked_up(hub, worker)
    _truncate(worker, tid, 200)
    worker.enter()
    path = wn._replica_path(worker.conn)
    raw = sqlite3.connect(path)
    with raw:
        raw.execute("DELETE FROM task_events WHERE task_id = ? AND kind = 'owner_admitted'", (tid,))
    raw.close()
    replica = _replica(worker)
    try:
        report = kph.refill(worker.conn, replica, wn._own(worker.conn), [tid])
    finally:
        replica.close()
    assert report[tid]["filled"] > 0
    run1 = worker.conn.execute("SELECT id FROM task_runs WHERE task_id = ? ORDER BY id LIMIT 1", (tid,)).fetchone()[0]
    assert run1 in _unadmitted_runs(worker.conn, tid)


def test_refill_history_cli(isolate_usage_accounting, gws, capsys):
    hub, worker = gws
    tid = _long_card_repicked_up(hub, worker)
    _truncate(worker, tid, 200)
    worker.enter()
    args = argparse.Namespace(board=BOARD, task_ids=[tid, "t_missing"], json=True)
    assert kanban_sync_cli.cmd_sync(argparse.Namespace(sync_action="refill-history", **vars(args))) == 1
    out = json.loads(capsys.readouterr().out)
    assert out[tid]["filled"] > 0 and out["t_missing"]["reason"] == "not here"
    assert _unadmitted_runs(worker.conn, tid) == []
    args.task_ids = [tid]
    assert kanban_sync_cli.cmd_sync(argparse.Namespace(sync_action="refill-history", **vars(args))) == 0
    assert json.loads(capsys.readouterr().out) == {tid: {"filled": 0, "unresolved": 0}}


def test_pickup_tick_refills_open_copies(isolate_usage_accounting, gws):
    hub, worker = gws
    tid = _long_card_repicked_up(hub, worker)
    _truncate(worker, tid, 200)
    worker.tick()
    assert _unadmitted_runs(worker.conn, tid) == []
