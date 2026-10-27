"""A new run must not inherit the previous worker's stale heartbeat."""

import signal

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.parametrize("source_status", ["ready", "review"])
@pytest.mark.parametrize("current_run_stale", [False, True], ids=["fresh", "stale"])
def test_reclaim_uses_current_run_heartbeat(board, monkeypatch, source_status, current_run_stale):
    conn = board
    now = 2_000_000_000
    old = now - kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS - 600
    clock = [old]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])
    tid = kb.create_task(conn, title="Wake a previously active task", assignee="worker")
    previous = kb.claim_task(conn, tid)
    assert previous is not None
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?", (old, tid))
    assert kb.reclaim_task(conn, tid, signal_fn=lambda *_: pytest.fail("No old worker to signal"))
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (source_status, tid))

    clock[0] = now
    claim = kb.claim_task if source_status == "ready" else kb.claim_review_task
    current = claim(conn, tid, ttl_seconds=60)
    assert current is not None
    assert current.current_run_id != previous.current_run_id
    # Task age is intentionally retained; only the activity baseline belongs to the new run.
    assert current.started_at == old
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                     (424242, "test-fingerprint", tid))

    alive = [True]
    monkeypatch.setattr(kb, "_worker_alive", lambda *_: alive[0])
    monkeypatch.setattr(kb, "_pid_alive", lambda *_: alive[0])
    monkeypatch.setattr(kbd, "_worker_alive", lambda *_: alive[0])
    monkeypatch.setattr(kbd, "_pid_recycled", lambda *_: False)
    signals = []

    def terminate(pid, sig):
        signals.append((pid, sig))
        alive[0] = False

    # Both claims expire, but only the control run has actually gone an hour without activity.
    clock[0] = now + (kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS + 1 if current_run_stale else 61)
    assert kb.release_stale_claims(conn, signal_fn=terminate) == int(current_run_stale)
    task = kb.get_task(conn, tid)
    run = conn.execute("SELECT * FROM task_runs WHERE id = ?", (current.current_run_id,)).fetchone()
    assert task.last_heartbeat_at == now
    if current_run_stale:
        assert signals == [(424242, signal.SIGTERM)]
        assert task.status == source_status
        assert task.claim_lock is None
        assert task.worker_pid is None
        assert run["outcome"] == "reclaimed"
        assert run["ended_at"] == clock[0]
    else:
        assert signals == []
        assert task.status == "running"
        assert task.current_run_id == current.current_run_id
        assert task.claim_lock == current.claim_lock
        assert task.claim_expires > clock[0]
        assert run["claim_expires"] == task.claim_expires
        assert run["ended_at"] is None
        assert run["outcome"] is None
