"""Foreign-parent mirror skips content-unchanged rounds, never recovery work."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_parent_refresh as gate
from tests.hermes_cli import test_kanban_move_parent_evidence as helpers

svc = helpers.svc
gws = helpers.gws
moved_parent = helpers.moved_parent
original_refresh = sync.refresh_remote_parents


@pytest.fixture
def mirror(moved_parent, monkeypatch):
    hub, worker, task, parent = moved_parent
    gate.reset()
    now = [100.0]
    monkeypatch.setattr(gate, "_clock", lambda: now[0])
    calls = []
    original = sync.refresh_remote_parents

    def refresh(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(sync, "refresh_remote_parents", refresh)
    def run(**kwargs):
        return gate.refresh(worker.conn, hub.conn, board="default", **kwargs)
    run()
    calls.clear()
    yield hub, worker, task, parent, now, calls, run
    gate.reset()


def test_unchanged_and_bookkeeping_do_not_refresh(mirror):
    hub, worker, task, parent, _now, calls, run = mirror
    assert run() == []
    worker.conn.execute("UPDATE tasks SET last_heartbeat_at=123 WHERE id=?", (task,))
    hub.conn.execute("UPDATE tasks SET last_heartbeat_at=124 WHERE id=?", (parent,))
    assert run() == []
    assert calls == []


@pytest.mark.parametrize("table,column,value", [
    ("tasks", "status", "ready"),
    ("task_runs", "outcome", "crashed"),
])
def test_remote_changes_rearm(mirror, table, column, value):
    hub, worker, _task, parent, _now, calls, run = mirror
    key = "id" if table == "tasks" else "task_id"
    hub.conn.execute(f"UPDATE {table} SET {column}=? WHERE {key}=?", (value, parent))
    assert parent in run()
    assert calls == []  # row-keyed: only the moved parent is re-read, not the whole wanted set
    assert original_refresh(worker.conn, hub.conn) == []  # the full path agrees: nothing left to change


def test_replica_deleted_parent_drops_rows_on_row_path(mirror):
    hub, worker, task, parent, _now, calls, run = mirror
    hub.conn.execute("DELETE FROM task_runs WHERE task_id=?", (parent,))
    hub.conn.execute("DELETE FROM tasks WHERE id=?", (parent,))
    assert parent in run()
    assert calls == []
    assert worker.conn.execute(f"SELECT 1 FROM {sync.REMOTE_PARENTS_TABLE} WHERE parent_id=?", (parent,)).fetchone() is None
    assert helpers.evidence.result(worker.conn, task, parent) is None
    assert original_refresh(worker.conn, hub.conn) == []


def test_local_unrelated_change_takes_row_path(mirror):
    hub, worker, task, parent, _now, calls, run = mirror
    hub.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
    worker.conn.execute("UPDATE tasks SET title='edited' WHERE id=?", (task,))
    assert parent in run()
    assert calls == []
    assert original_refresh(worker.conn, hub.conn) == []


def _mirrored(worker, parent):
    status = worker.conn.execute(
        f"SELECT 1 FROM {sync.REMOTE_PARENTS_TABLE} WHERE parent_id=?", (parent,)).fetchone()
    return status is not None, worker.conn.execute(
        "SELECT 1 FROM kanban_move_parent_results WHERE parent_id=?", (parent,)).fetchone() is not None


def test_imported_then_deleted_parent_moves_child_scope_on_row_path(mirror):
    from hermes_cli import kanban_move_parent_evidence as evidence

    hub, worker, task, parent, _now, calls, run = mirror
    assert _mirrored(worker, parent) == (True, True)
    assert parent in evidence._parked_scope_uncached(worker.conn)
    # Imported: the parent becomes a local card, so its child leaves the foreign-parent scope (p.id IS NULL).
    worker.conn.execute("INSERT INTO tasks (id, title, status, created_at) VALUES (?, 'imported', 'todo', 1)",
                        (parent,))
    assert parent in run()
    assert calls == []
    assert parent not in evidence._parked_scope_uncached(worker.conn)
    assert _mirrored(worker, parent) == (False, False)
    assert original_refresh(worker.conn, hub.conn) == []
    # Deleted again: the child is back under a foreign parent, and both mirror rows return without a full pass.
    worker.conn.execute("DELETE FROM tasks WHERE id=?", (parent,))
    assert parent in run()
    assert calls == []
    assert parent in evidence._parked_scope_uncached(worker.conn)
    assert _mirrored(worker, parent) == (True, True)
    assert helpers.evidence.result(worker.conn, task, parent)
    assert original_refresh(worker.conn, hub.conn) == []


def test_local_link_to_new_foreign_parent_on_row_path(mirror):
    hub, worker, _task, _parent, _now, calls, run = mirror
    other = "t_newforeignparent"
    hub.conn.execute("INSERT INTO tasks (id, title, status, created_at) VALUES (?, 'p', 'running', 1)", (other,))
    run()
    calls.clear()
    worker.conn.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_newchild', 'c', 'todo', 1)")
    worker.conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, 't_newchild')", (other,))
    assert other in run()
    assert calls == []
    assert _mirrored(worker, other) == (True, False)  # wanted, but no park or queued move scopes its evidence
    assert original_refresh(worker.conn, hub.conn) == []
    worker.conn.execute("DELETE FROM task_links WHERE child_id='t_newchild'")
    assert other in run()
    assert calls == []
    assert _mirrored(worker, other) == (False, False)
    assert original_refresh(worker.conn, hub.conn) == []


def test_row_path_keeps_unscoped_parent_out_of_evidence(mirror):
    hub, worker, _task, _parent, _now, calls, run = mirror
    other = "t_unscopedparent"
    hub.conn.execute("INSERT INTO tasks (id, title, status, created_at) VALUES (?, 'p', 'done', 1)", (other,))
    hub.conn.execute("INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
                     "VALUES (?, 'done', 'completed', 1, 2)", (other,))
    worker.conn.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_unparkedchild', 'c', 'todo', 1)")
    worker.conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, 't_unparkedchild')", (other,))
    assert other in run()  # local link: the row path learns the new wanted parent
    calls.clear()
    hub.conn.execute("UPDATE task_runs SET outcome='crashed' WHERE task_id=?", (other,))
    hub.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (other,))
    assert other in run()
    assert calls == []
    assert worker.conn.execute("SELECT 1 FROM kanban_move_parent_results WHERE parent_id=?", (other,)).fetchone() is None
    assert original_refresh(worker.conn, hub.conn) == []


def test_row_path_keeps_floor_anchor(mirror):
    hub, worker, task, parent, now, calls, run = mirror
    worker.conn.execute("DELETE FROM kanban_move_parent_results")
    now[0] += gate.MAX_AGE_S - 10
    hub.conn.execute("UPDATE tasks SET title='unrelated' WHERE id=?", (task,))
    assert run() == []  # a replica row outside the wanted set: nothing to re-read
    assert calls == []
    now[0] += 10  # 30s after the last FULL path, not after the row-keyed round
    assert parent in run()
    assert calls == [True]
    assert helpers.evidence.result(worker.conn, task, parent)


def test_local_park_event_rearms_without_feed(mirror):
    hub, worker, task, parent, _now, calls, run = mirror
    worker.conn.execute("DELETE FROM task_events WHERE task_id=? AND kind IN ('dependency_wait','moved')", (task,))
    assert parent in run()
    assert calls == []  # row path: the parent left the evidence scope the previous round read
    assert _mirrored(worker, parent) == (True, False)
    assert original_refresh(worker.conn, hub.conn) == []


def test_mirror_clobber_recovers_at_floor_without_pull(mirror):
    _hub, worker, task, parent, now, calls, run = mirror
    worker.conn.execute("DELETE FROM kanban_move_parent_results")
    assert run() == []
    assert calls == []
    now[0] += gate.MAX_AGE_S
    assert parent in run()
    assert calls == [True]
    assert helpers.evidence.result(worker.conn, task, parent)


def test_failure_does_not_publish_skip(mirror, monkeypatch):
    _hub, _worker, _task, _parent, now, calls, run = mirror
    now[0] += gate.MAX_AGE_S
    original = sync.refresh_remote_parents
    monkeypatch.setattr(sync, "refresh_remote_parents",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("failure")))
    with pytest.raises(RuntimeError, match="failure"):
        run()
    monkeypatch.setattr(sync, "refresh_remote_parents", original)
    run()
    assert calls == [True]


def test_transaction_bypasses_memo_and_rollback_does_not_poison(mirror):
    _hub, worker, task, parent, _now, calls, run = mirror
    worker.conn.execute("BEGIN")
    worker.conn.execute("DELETE FROM task_events WHERE task_id=? AND kind IN ('dependency_wait','moved')", (task,))
    with pytest.raises(RuntimeError, match="already inside a transaction"):
        run()  # underlying writer keeps its no-nesting contract; no memo is published
    worker.conn.execute("ROLLBACK")
    run()
    assert calls == [True, True]
    assert helpers.evidence.result(worker.conn, task, parent)


_PARK_KINDS = "('dependency_wait','moved')"


def _park_rows(conn, task):
    return conn.execute(f"SELECT * FROM task_events WHERE task_id=? AND kind IN {_PARK_KINDS} ORDER BY id",
                        (task,)).fetchall()


def _restore(conn, rows):
    for r in rows:
        cols = list(r.keys())
        conn.execute(f"INSERT INTO task_events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", tuple(r))


def test_full_path_stamps_the_scope_it_applied(mirror, monkeypatch):
    """A scope that drops a parent while the full path runs, then regains it, still re-reads that parent."""
    hub, worker, task, parent, now, calls, run = mirror
    saved = _park_rows(worker.conn, task)
    now[0] += gate.MAX_AGE_S
    real = helpers.evidence._evidence_scope
    reads = [0]

    def scope(live):
        reads[0] += 1
        if reads[0] == 2:  # a second read inside the full path would see the child's park gone
            live.execute(f"DELETE FROM task_events WHERE task_id=? AND kind IN {_PARK_KINDS}", (task,))
        return real(live)

    monkeypatch.setattr(helpers.evidence, "_evidence_scope", scope)
    run()
    monkeypatch.setattr(helpers.evidence, "_evidence_scope", real)
    assert calls == [True]
    deleted = reads[0] >= 2
    if deleted:
        _restore(worker.conn, saved)
    assert reads[0] == 1  # the full path builds the mirror from the very scope the gate stamps
    calls.clear()
    run()
    assert calls == []
    assert _mirrored(worker, parent) == (True, True)
    assert original_refresh(worker.conn, hub.conn) == []


def test_parent_reenters_scope_on_row_path(mirror):
    hub, worker, task, parent, _now, calls, run = mirror
    saved = _park_rows(worker.conn, task)
    worker.conn.execute(f"DELETE FROM task_events WHERE task_id=? AND kind IN {_PARK_KINDS}", (task,))
    run()
    assert _mirrored(worker, parent) == (True, False)
    _restore(worker.conn, saved)
    assert parent in run()
    assert calls == []
    assert _mirrored(worker, parent) == (True, True)
    assert original_refresh(worker.conn, hub.conn) == []


def test_child_done_leaves_scope_on_row_path(mirror):
    hub, worker, task, parent, _now, calls, run = mirror
    worker.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task,))
    assert parent in run()
    assert calls == []
    assert _mirrored(worker, parent) == (True, False)
    assert original_refresh(worker.conn, hub.conn) == []


def test_replica_claimed_event_on_row_path(mirror):
    hub, worker, _task, parent, _now, calls, run = mirror
    rid = hub.conn.execute("SELECT MAX(id) FROM task_runs WHERE task_id=?", (parent,)).fetchone()[0]
    hub.conn.execute("DELETE FROM task_events WHERE task_id=? AND run_id=? AND kind='claimed'", (parent, rid))
    assert parent in run()
    assert calls == []
    assert original_refresh(worker.conn, hub.conn) == []


def test_replica_run_bookkeeping_is_not_mirrored(mirror):
    """The P2 cursor never marks bookkeeping run columns, so the mirror must not carry them either."""
    hub, worker, task, parent, now, calls, run = mirror
    hub.conn.execute("UPDATE task_runs SET last_heartbeat_at=999, worker_pid=4242 WHERE task_id=?", (parent,))
    assert run() == []
    assert original_refresh(worker.conn, hub.conn) == []
    now[0] += gate.MAX_AGE_S
    assert run() == []  # the floor's full path finds nothing either: no spurious dispatcher wake
    assert calls == [True]
    mirrored = helpers.evidence.result(worker.conn, task, parent)
    assert mirrored and "last_heartbeat_at" not in mirrored and "worker_pid" not in mirrored
