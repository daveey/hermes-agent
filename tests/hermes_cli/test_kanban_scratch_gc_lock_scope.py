"""Scratch GC must not hold the board's write lock across tree walks and rmtree
(t_01a1d3ac14d0b207): a ``kanban gc`` on a loaded Mac held BEGIN IMMEDIATE for 11 minutes while
it removed 29 workspaces. Each removal re-checks its owner in a short txn instead."""
import shutil
import sqlite3
import threading
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_worktree_gc as gc
from tests.hermes_cli import test_kanban_scratch_gc_archive as helpers

board = helpers.board


def _db_path():
    with kbc.connect_closing() as conn:
        return conn.execute("PRAGMA database_list").fetchone()["file"]


def _run_gc():
    with kbc.connect_closing() as conn:
        return gc.gc_task_scratch(conn, older_than_days=7)


def test_board_writable_while_scratch_gc_removes_trees(board, monkeypatch):
    """Test C: another connection's BEGIN IMMEDIATE succeeds while a slow rmtree runs."""
    paths = [helpers._task()[1] for _ in range(3)]
    db, real_rmtree = _db_path(), shutil.rmtree
    waits = []

    def slow_rmtree(path, *args, **kwargs):
        other = sqlite3.connect(db, timeout=0.5, isolation_level=None)
        started = time.monotonic()
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            waits.append(time.monotonic() - started)
        except sqlite3.OperationalError as exc:
            waits.append(exc)
        finally:
            other.close()
        threading.Event().wait(1.0)  # health: allow HX013 -- a slow rmtree IS the behaviour under test
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", slow_rmtree)
    assert _run_gc() == 3
    assert len(waits) == 3
    assert all(isinstance(w, float) and w < 0.5 for w in waits), waits
    assert not any(p.exists() for p in paths)
    root = kb.workspaces_root()
    assert not [p for p in root.iterdir() if ".gc-rm-" in p.name]


def test_card_resumed_mid_gc_keeps_its_workspace(board, monkeypatch):
    """Test C: the per-removal ownership re-check skips a card resumed after selection."""
    tasks = [helpers._task() for _ in range(3)]
    db, real_rmtree = _db_path(), shutil.rmtree
    resumed: list[str] = []

    def rmtree_then_resume_the_rest(path, *args, **kwargs):
        if not resumed:
            other = sqlite3.connect(db, timeout=0.5, isolation_level=None)
            try:
                for tid, tpath in tasks:
                    if tpath.exists():
                        other.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
                        resumed.append(tid)
            finally:
                other.close()
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree_then_resume_the_rest)
    assert _run_gc() == 1
    assert len(resumed) == 2
    for tid, path in tasks:
        assert path.exists() == (tid in resumed)


def test_owner_repointed_mid_gc_keeps_the_tree(board, monkeypatch):
    tid, path = helpers._task()
    db = _db_path()

    def repoint_then_walk(p):
        other = sqlite3.connect(db, timeout=0.5, isolation_level=None)
        try:
            other.execute("UPDATE tasks SET workspace_path='/elsewhere' WHERE id=?", (tid,))
        finally:
            other.close()
        return True

    monkeypatch.setattr(gc.ws, "_scratch_tree_removable", repoint_then_walk)
    assert _run_gc() == 0
    assert path.exists()
