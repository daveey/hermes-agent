"""GC retries deferred terminal scratch cleanup without guessing ownership."""
import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_ops


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    return tmp_path


def _workspace(status="done"):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="deferred scratch")
        path = kb.workspaces_root() / tid
        path.mkdir(parents=True)
        (path / "artifact.txt").write_text("scratch")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status=?, workspace_kind='scratch', workspace_path=? WHERE id=?",
                (status, str(path), tid),
            )
    return tid, path


def _gc():
    return kanban_ops._cmd_gc(argparse.Namespace(
        event_retention_days=0, log_retention_days=0, worktree_retention_days=0,
    ))


@pytest.mark.parametrize("status", ["done", "archived", "failed", "cancelled"])
def test_gc_retries_terminal_scratch(board, status):
    _, path = _workspace(status)
    assert _gc() == 0
    assert not path.exists()


@pytest.mark.parametrize("status", ["ready", "running", "blocked", "review", "scheduled"])
def test_gc_keeps_nonterminal_and_unknown_scratch(board, status):
    _, path = _workspace(status)
    unknown = kb.workspaces_root() / "t_unknown"
    unknown.mkdir()
    assert _gc() == 0
    assert path.exists()
    assert unknown.exists()


@pytest.mark.parametrize("metadata", ["dotgit", "bare", "symlink"])
def test_gc_preserves_uncertain_scratch(board, metadata):
    _, path = _workspace("archived")
    nested = path / "nested"
    nested.mkdir()
    if metadata == "dotgit":
        (nested / ".git").write_text("gitdir: elsewhere")
    elif metadata == "bare":
        for name in ("HEAD", "objects", "refs"):
            (nested / name).mkdir()
    else:
        (nested / "link").symlink_to(path / "artifact.txt")
    assert _gc() == 0
    assert (path / "artifact.txt").exists()


def test_gc_retries_after_cross_board_sharer_finishes(board):
    _, path = _workspace()
    kb.create_board("other")
    with kbc.connect_closing(board="other") as conn:
        sharer = kb.create_task(conn, title="live sharer")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(path), sharer))
    assert _gc() == 0
    assert path.exists()
    with kbc.connect_closing(board="other") as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (sharer,))
    assert _gc() == 0
    assert not path.exists()


def test_gc_preserves_parent_with_active_child(board):
    tid, path = _workspace()
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="needs artifacts", parents=[tid])
    assert _gc() == 0
    assert path.exists()


def test_gc_reads_all_boards_before_removing_any_scratch(board, monkeypatch):
    from hermes_cli import kanban_db_workspace as ws

    scanned = []
    monkeypatch.setattr(ws, "_scratch_tree_removable", lambda path: scanned.append(path) or True)
    _, first = _workspace()
    _, second = _workspace("archived")
    kb.create_board("broken")
    broken = kb.kanban_home() / "kanban" / "boards" / "broken" / "kanban.db"
    broken.write_bytes(b"not sqlite")
    assert _gc() == 0
    assert first.exists()
    assert second.exists()
    assert scanned == []


def test_gc_keeps_scratch_when_board_discovery_fails(board, monkeypatch):
    from hermes_cli import kanban_db_workspace as ws

    _, path = _workspace()

    def unreadable(_conn):
        raise PermissionError("cannot enumerate boards")

    monkeypatch.setattr(ws, "_sibling_board_db_files", unreadable)
    assert _gc() == 0
    assert path.exists()


@pytest.mark.parametrize("entry", ["gc", "completion"])
@pytest.mark.parametrize("relation", ["descendant", "ancestor", "sibling"])
def test_cleanup_protects_overlapping_live_workspaces(board, entry, relation):
    from hermes_cli import kanban_db_workspace as ws

    tid, path = _workspace()
    live_path = {"descendant": path / "nested", "ancestor": path.parent,
                 "sibling": path.with_name(path.name + "-other")}[relation]
    live_path.mkdir(parents=True, exist_ok=True)
    with kbc.connect_closing() as conn:
        live = kb.create_task(conn, title="live overlapping directory")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id=?",
                         (str(live_path), live))
        if entry == "completion":
            ws._cleanup_workspace(conn, tid)
    if entry == "gc":
        assert _gc() == 0
    assert path.exists() is (relation != "sibling")
    assert live_path.exists()
