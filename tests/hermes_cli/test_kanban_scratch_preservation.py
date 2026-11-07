"""Scratch preservation is observable without granting deletion authority."""
import logging
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_worktree_gc as gc
from tests.hermes_cli import test_kanban_scratch_gc_archive as helpers

board = helpers.board
remote = helpers.remote


def test_moved_owner_absent_locally_is_preserved(board, caplog):
    path = kb.workspaces_root() / "t_0123456789abcdef"
    path.mkdir(parents=True)
    (path / "notes").write_text("keep")
    # A sync shadow is not a local ownership or deletion authority.
    shadow = kb.kanban_home() / "kanban" / "sync-shadow" / "default.db"
    shadow.parent.mkdir(parents=True)
    with sqlite3.connect(shadow) as conn:
        conn.execute("CREATE TABLE tasks (id, status, workspace_kind, workspace_path)")
        conn.execute("INSERT INTO tasks VALUES (?, 'done', 'scratch', NULL)", (path.name,))
    caplog.set_level(logging.WARNING)
    assert helpers._gc() == 0
    assert (path / "notes").read_text() == "keep"
    assert f"reason=owner_unverified path={path}" in caplog.text
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("days,reason", [(7, "retention_pending"), (0, "retention_disabled")])
def test_known_clone_reports_stable_retention(board, remote, caplog, days, reason):
    finished = int(time.time()) - 3600
    tid, path = helpers._task(remote=remote, completed_at=finished)
    before = helpers._events(tid)
    caplog.set_level(logging.WARNING)
    for _ in range(2):
        assert helpers._gc(days=days) == 0
    assert path.exists()
    assert f"reason={reason} path={path}" in caplog.text
    if days:
        assert f"eligible_at={finished + days * 86400}" in caplog.text
    assert helpers._events(tid) == before


def test_actual_nonterminal_owner_is_not_unknown(board, caplog):
    _, path = helpers._task(status="running")
    caplog.set_level(logging.WARNING)
    assert helpers._gc() == 0
    assert path.exists()
    assert "owner_unverified" not in caplog.text


def test_sibling_record_and_null_path_owner_are_known(board, caplog):
    tid, path = helpers._task(status="running")
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_path=NULL WHERE id=?", (tid,))
    other_path = path.parent / "not-a-task-id"
    other_path.mkdir()
    kb.create_board("other")
    with kbc.connect_closing(board="other") as conn:
        sibling = kb.create_task(conn, title="sibling owner")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(other_path), sibling))
    caplog.set_level(logging.WARNING)
    assert helpers._gc() == 0
    assert path.exists() and other_path.exists()
    assert "owner_unverified" not in caplog.text


def test_inventory_does_not_follow_symlinks_or_classify_files(board, caplog):
    root = kb.workspaces_root()
    root.mkdir(parents=True)
    (root / "link").symlink_to(board, target_is_directory=True)
    (root / "file").write_text("not a workspace")
    caplog.set_level(logging.WARNING)
    assert helpers._gc() == 0
    assert "owner_unverified" not in caplog.text
    assert (root / "link").is_symlink()


def test_unreadable_board_aborts_without_unknown_owner_claim(board, caplog):
    _, path = helpers._task()
    kb.create_board("broken")
    kb.board_own_db_path("broken").write_bytes(b"not sqlite")
    caplog.set_level(logging.WARNING)
    assert helpers._gc() == 0
    assert path.exists()
    assert "cannot read all boards" in caplog.text
    assert "owner_unverified" not in caplog.text


def test_nested_record_represents_root_child(board, caplog):
    tid, path = helpers._task(status="running")
    nested = path / "nested"
    nested.mkdir()
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(nested), tid))
    caplog.set_level(logging.WARNING)
    assert helpers._gc() == 0
    assert "owner_unverified" not in caplog.text


def test_inventory_failure_does_not_block_known_cleanup(board, caplog, monkeypatch):
    _, path = helpers._task()
    real_iterdir = type(path).iterdir

    def unreadable_root(self):
        if self == path.parent:
            raise PermissionError("inventory denied")
        return real_iterdir(self)

    monkeypatch.setattr(type(path), "iterdir", unreadable_root)
    caplog.set_level(logging.WARNING)
    assert helpers._gc() == 0
    assert not path.exists()
    assert "inventory unavailable" in caplog.text
    assert "cannot read all boards" not in caplog.text


def test_completed_time_wins_over_later_diagnostics(board, remote, caplog):
    tid, path = helpers._task(remote=remote)
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "commented", {"body": "diagnostic"})
        assert gc.gc_task_scratch(conn, older_than_days=7) == 1
    assert not path.exists()
