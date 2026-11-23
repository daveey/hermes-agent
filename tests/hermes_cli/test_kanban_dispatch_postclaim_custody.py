"""Real process custody survives a preclaim-only writer allowance."""
import contextlib
import sqlite3
import threading
import subprocess
import sys
import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_dispatch_storage as storage
from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml


@pytest.mark.parametrize('budgeted', [True, False])
@pytest.mark.parametrize('count', [1, 2])
def test_postclaim_pid_publication_retains_custody(tmp_path, monkeypatch, budgeted, count):
    from pathlib import Path
    from hermes_cli import kanban_model_retirement, kanban_ratio_routing
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    (tmp_path / 'config.yaml').write_text(_managed_yaml())
    monkeypatch.setattr(kanban_model_retirement, 'sweep', lambda _conn: None)
    monkeypatch.setattr(kanban_ratio_routing, 'begin_tick', lambda _conn: None)
    monkeypatch.setattr(dispatch, 'reap_worker_zombies', lambda: None)
    monkeypatch.setattr(dispatch, 'reap_terminal_workers', lambda _conn: [])
    monkeypatch.setattr(storage, 'WRITE_WAIT_SECONDS', 0.0)
    if not budgeted:
        monkeypatch.setattr(storage, 'run', lambda _conn, work, *, board: work())
    path = tmp_path / 'kanban.db'
    threads = []
    children = []
    with contextlib.closing(kbc.connect(path)) as conn, contextlib.closing(
            sqlite3.connect(path, timeout=0, check_same_thread=False)) as writer:
        tids = [kb.create_task(conn, title=f'postclaim custody {index}', assignee='default') for index in range(count)]
        with kb.write_txn(conn):
            conn.executemany("UPDATE tasks SET status='ready' WHERE id=?", [(tid,) for tid in tids])
        conn.execute('PRAGMA busy_timeout=500')
        began = threading.Event()

        def spawn(*_args):
            writer.execute('BEGIN IMMEDIATE')
            began.set()

            def release():
                threading.Event().wait(0.05)  # health: allow HX013 -- contention must outlast zero preclaim budget
                writer.rollback()

            thread = threading.Thread(target=release)
            threads.append(thread)
            child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'], stdin=subprocess.PIPE)
            children.append(child)
            thread.start()
            return child.pid

        try:
            result = dispatch.dispatch_once(conn, spawn_fn=spawn, reconcile_orphans=False,
                                            max_in_progress=16, max_spawn=count)
            assert began.is_set(), result
            assert set(row[0] for row in result.spawned) == set(tids), result
            stored_pids = []
            for tid, _assignee, _workspace in result.spawned:
                task = kb.get_task(conn, tid)
                assert task is not None and task.status == 'running'
                stored_pids.append(conn.execute('SELECT worker_pid FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0])
            assert set(stored_pids) == {child.pid for child in children}
            assert result.storage_skip_reason is None
            assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 500
        finally:
            for thread in threads:
                thread.join(timeout=2)
                assert not thread.is_alive()
            for child in children:
                child.stdin.close()
                child.wait(timeout=3)


def test_ready_with_free_slots_records_storage_reason_then_recovers(tmp_path, monkeypatch, caplog):
    from pathlib import Path
    from hermes_cli import kanban_model_retirement, kanban_ratio_routing
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    (tmp_path / 'config.yaml').write_text(_managed_yaml())
    monkeypatch.setattr(kanban_model_retirement, 'sweep', lambda _conn: None)
    monkeypatch.setattr(kanban_ratio_routing, 'begin_tick', lambda _conn: None)
    monkeypatch.setattr(dispatch, 'reap_worker_zombies', lambda: None)
    monkeypatch.setattr(dispatch, 'reap_terminal_workers', lambda _conn: [])
    monkeypatch.setattr(storage, 'WRITE_WAIT_SECONDS', 0.0)
    path = tmp_path / 'kanban.db'
    with contextlib.closing(kbc.connect(path)) as conn, contextlib.closing(sqlite3.connect(path)) as writer:
        tid = kb.create_task(conn, title='ready storage interleave', assignee='default')
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        spawned = []
        writer.execute('BEGIN IMMEDIATE')
        try:
            result = dispatch.dispatch_once(conn, spawn_fn=lambda *args: spawned.append(args),
                                            reconcile_orphans=False, max_in_progress=16, max_spawn=1)
            assert result.storage_skip_reason == storage.REASON
            assert storage.REASON in caplog.text
            assert not spawned and not result.spawned
            assert kb.get_task(conn, tid).status == 'ready'
            assert conn.execute('SELECT count(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0] == 0
        finally:
            writer.rollback()
        result = dispatch.dispatch_once(conn, spawn_fn=lambda *args: spawned.append(args),
                                        reconcile_orphans=False, max_in_progress=16, max_spawn=1)
        assert [row[0] for row in result.spawned] == [tid]
        assert len(spawned) == 1
        assert result.storage_skip_reason is None

