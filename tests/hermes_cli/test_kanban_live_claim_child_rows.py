"""A Board ``live_claim`` refusal of a run handoff or lifecycle event is recorded and undone locally. [daveey]

The Board (hermes-hub #111) refuses a non-holder's run handoff and lifecycle events by the same live-lease rule as
the task row. Before it, core <= e43a9e016a (still on the MacBook) completed a card another machine ran: the task
row came back ``live_claim`` but the run end and the ``completed`` event reached the Board (t_01a1d5386cb55abf).
Here the hub writes that completion the way an older core did (raw rows, past the up-front guards of PR #2254) and
pushes it to a Board that fences them: every refusal is recorded on the card and, on a primary board, the local
run is restored from the Board's row and a phantom run or event the Board never stored is removed.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_remote_archive import _running_on_worker
from tests.hermes_cli.test_kanban_worker_node import BOARD, HUB, WORKER, Gateway


@pytest.fixture
def svc():
    return FakeService(lease_runs=True)


@pytest.fixture
def gws(tmp_path, monkeypatch, svc):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_DB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    hub = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    worker = Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="true")
    yield hub, worker
    for g in (hub, worker):
        g.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _old_core_complete(conn, tid, *, phantom=False):
    """What core's complete_task wrote before the up-front guards: the task row, the run end (or, with no local
    run, a synthesized one) and a ``completed`` event. The capture triggers queue all three."""
    with conn:
        conn.execute("UPDATE tasks SET status = 'done', result = 'X', completed_at = 2000 WHERE id = ?", (tid,))
        if phantom:
            cur = conn.execute("INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at, summary)"
                               " VALUES (?, 'default', 'done', 'completed', 2000, 2000, 'X')", (tid,))
            run_id = cur.lastrowid
        else:
            run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
            run_id = run_id or conn.execute("SELECT MAX(id) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0]
            conn.execute("UPDATE task_runs SET status = 'done', outcome = 'completed', ended_at = 2000,"
                         " summary = 'X', metadata = '{\"by\": \"hub\"}' WHERE id = ?", (run_id,))
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                     " VALUES (?, ?, 'completed', '{\"summary\": \"X\"}', 2000)", (tid, run_id))
    return run_id


def _rejections(conn, tid):
    return [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'sync_rejected' ORDER BY id", (tid,))]


def _board_runs(svc, tid):
    return {u: r for u, r in svc.board(BOARD)["rows"].get("task_runs", {}).items() if r.get("task_id") == tid}


def _board_kinds(svc, tid):
    return [e["kind"] for e in svc.board(BOARD)["rows"].get("task_events", {}).values() if e.get("task_id") == tid]


def test_refused_run_handoff_is_recorded_and_the_local_run_restored(gws, svc):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    run_id = _old_core_complete(hub.conn, tid)
    uid = hub.conn.execute("SELECT uid FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0]
    hub.round()
    # Board: nothing of the completion landed.
    assert _board_runs(svc, tid)[uid]["status"] == "running" and _board_runs(svc, tid)[uid]["summary"] is None
    assert "completed" not in _board_kinds(svc, tid)
    # Hub: each refused row is recorded on the card, naming the table ...
    rej = _rejections(hub.conn, tid)
    assert {(r.get("table", "tasks"), r["reason"].split(":")[0]) for r in rej} >= {
        ("tasks", "live_claim"), ("task_runs", "live_claim"), ("task_events", "live_claim")}, rej
    # ... the run mirror is the Board's again, and the refused `completed` event is gone.
    run = hub.conn.execute("SELECT status, outcome, ended_at, summary, metadata FROM task_runs WHERE uid = ?",
                           (uid,)).fetchone()
    assert tuple(run) == ("running", None, None, None, None)
    assert hub.conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'completed'",
                            (tid,)).fetchone()[0] == 0
    # The next rounds re-upload none of it, and the holder's own completion then applies normally.
    hub.round()
    assert _board_runs(svc, tid)[uid]["summary"] is None
    worker.enter()
    wrid = worker.conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    assert kb.complete_task(worker.conn, tid, summary="worker's handoff", expected_run_id=wrid)
    worker.round()
    hub.round()
    assert _board_runs(svc, tid)[uid]["summary"] == "worker's handoff"
    assert hub.conn.execute("SELECT summary FROM task_runs WHERE uid = ?", (uid,)).fetchone()[0] == "worker's handoff"


def test_refused_phantom_run_is_removed_and_never_reuploaded(gws, svc):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    before = set(_board_runs(svc, tid))
    run_id = _old_core_complete(hub.conn, tid, phantom=True)
    hub.round()
    hub.round()
    assert set(_board_runs(svc, tid)) == before
    assert hub.conn.execute("SELECT COUNT(*) FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0] == 0
    assert ("task_runs", "live_claim") in {(r.get("table", "tasks"), r["reason"].split(":")[0]) for r in _rejections(hub.conn, tid)}


def test_shadow_board_records_the_refusal_and_leaves_its_rows(gws, svc):
    """Non-primary: the refusal is still recorded; the live rows stay (the shadow follows the feed, as for tasks)."""
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    run_id = _old_core_complete(hub.conn, tid)
    sync.set_mode(hub.conn, "shadow")
    hub.round()
    assert ("task_runs", "live_claim") in {(r.get("table", "tasks"), r["reason"].split(":")[0]) for r in _rejections(hub.conn, tid)}
    assert hub.conn.execute("SELECT summary FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0] == "X"
    assert "completed" not in _board_kinds(svc, tid)


def test_old_wire_shape_task_only_handling_sees_extra_final_rejections(gws, svc, monkeypatch):
    """An older client (task-row-only live_claim handling) sends the same wire shape; the extra rejections are
    final, so its push ends after one round with nothing applied and nothing left to retry."""
    from hermes_cli import kanban_sync_live_claim as lc
    monkeypatch.setattr(lc, "RECORDED_TABLES", frozenset({"tasks"}))
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    _old_core_complete(hub.conn, tid)
    sends = []
    monkeypatch.setattr(svc, "calls", sends)
    hub.round()
    pushed = [ops for kind, _b, ops in sends if kind == "ops"]
    assert pushed, sends
    assert set(pushed[0][0]) >= {"client_op_id", "op", "table", "uid", "fields", "hlc"}
    assert "completed" not in _board_kinds(svc, tid)
    hub.round()
    assert "completed" not in _board_kinds(svc, tid)
    # Final, not transient: nothing of the refused completion is left pending for a later retry.
    assert hub.conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE state = 'pending'").fetchone()[0] == 0
    # The older client recorded only the task row; the extra refusals changed nothing else for it.
    assert {r.get("table", "tasks") for r in _rejections(hub.conn, tid)} == {"tasks"}
