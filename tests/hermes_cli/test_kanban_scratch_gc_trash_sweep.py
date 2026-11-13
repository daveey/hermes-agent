"""PR #1982 review follow-ups for scratch GC (t_01a1d4036f42d021).

A detached ``.<id>.gc-rm-<hex>`` tree whose rmtree failed is retried by the next GC run (outside
the board's write lock) and never reported as unowned scratch; a board that cannot be read at the
candidate SELECT aborts the run with the usual log line instead of raising.
"""
import contextlib
import logging
import shutil
import sqlite3
import threading
import time
import uuid

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_worktree_gc as gc
from tests.hermes_cli import test_kanban_scratch_gc_archive as helpers

board = helpers.board


def _run_gc():
    with kbc.connect_closing() as conn:
        return gc.gc_task_scratch(conn, older_than_days=7)


def _stale_trash(name="t_gone"):
    root = kb.workspaces_root()
    root.mkdir(parents=True, exist_ok=True)
    trash = root / f".{name}.gc-rm-{uuid.uuid4().hex}"
    (trash / "sub").mkdir(parents=True)
    (trash / "sub" / "file.txt").write_text("left over")
    return trash


def test_stale_detached_tree_is_swept_and_not_reported(board, caplog):
    trash = _stale_trash()
    with caplog.at_level(logging.WARNING, logger=gc.log.name):
        assert _run_gc() == 0
    assert not trash.exists()
    assert "owner_unverified" not in caplog.text


def test_sweep_leaves_lookalike_names_alone(board, caplog):
    root = kb.workspaces_root()
    root.mkdir(parents=True, exist_ok=True)
    keep = [root / ".t_x.gc-rm-notahexsuffix", root / "t_x.gc-rm-" f"{uuid.uuid4().hex}"]
    for path in keep:
        path.mkdir()
    real = root / "real-target"
    real.mkdir()
    (real / "precious.txt").write_text("keep")
    link = root / f".t_link.gc-rm-{uuid.uuid4().hex}"
    link.symlink_to(real, target_is_directory=True)
    _run_gc()
    assert all(p.is_dir() for p in keep)
    assert (real / "precious.txt").exists()
    assert link.is_symlink()


def test_undeletable_trash_is_reported_as_detached_not_unowned(board, caplog, monkeypatch):
    trash = _stale_trash()
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)
    with caplog.at_level(logging.WARNING, logger=gc.log.name):
        _run_gc()
    assert trash.exists()
    assert "owner_unverified" not in caplog.text
    assert f"detached tree {trash}" in caplog.text


def test_board_writable_while_stale_trash_is_swept(board, monkeypatch):
    _stale_trash()
    with kbc.connect_closing() as conn:
        db = conn.execute("PRAGMA database_list").fetchone()["file"]
    real_rmtree, waits = shutil.rmtree, []

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
        threading.Event().wait(0.2)  # health: allow HX013 -- a slow rmtree IS the behaviour under test
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", slow_rmtree)
    _run_gc()
    assert waits and all(isinstance(w, float) and w < 0.5 for w in waits), waits


def test_unreadable_board_at_candidate_select_aborts_with_log(board, caplog, monkeypatch):
    _tid, path = helpers._task()

    @contextlib.contextmanager
    def locked(conn, *a, **k):
        raise sqlite3.OperationalError("database is locked")
        yield  # pragma: no cover

    monkeypatch.setattr(kb, "write_txn", locked)
    with caplog.at_level(logging.WARNING, logger=gc.log.name):
        assert _run_gc() == 0
    assert "Scratch GC aborted" in caplog.text
    assert path.exists()
