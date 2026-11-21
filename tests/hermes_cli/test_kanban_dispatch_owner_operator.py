"""Operator changes serialize with real ticks and preserve ownership on rename."""
import argparse
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
from hermes_cli.kanban_boards import _dispatch_boards
from hermes_cli.kanban_dispatch_owner import get_dispatch_owner, set_dispatch_owner


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    (tmp_path / "config.yaml").write_text(_managed_yaml())
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb.create_board("sample")
    return Path(kb.kanban_db_path("sample"))


def test_operator_set_read_clear_and_rename(board, capsys):
    def command(owner=None, clear=False):
        return _dispatch_boards(argparse.Namespace(
            boards_action="dispatch-owner", slug="sample", owner=owner, clear=clear, json=True))
    assert command("worker") == 0
    assert json.loads(capsys.readouterr().out)["dispatch_owner"] == "worker"
    kb.write_board_metadata("sample", name="Changed")
    assert command() == 0
    assert json.loads(capsys.readouterr().out)["dispatch_owner"] == "worker"
    assert command(clear=True) == 0
    assert json.loads(capsys.readouterr().out)["dispatch_owner"] is None


def test_owner_change_and_display_write_refused_during_tick(board, monkeypatch):
    set_dispatch_owner(board, "worker")
    with kbc.connect_closing(board) as conn:
        task = kb.create_task(conn, title="One scoped spawn", assignee="default")
        def spawn(task, workspace, board=None):
            path = kb.kanban_db_path("sample")
            with pytest.raises(RuntimeError):
                set_dispatch_owner(path, "other")
            with pytest.raises(RuntimeError):
                kb.write_board_metadata("sample", name="Race")
            assert get_dispatch_owner(path) == "worker"
        monkeypatch.setattr(kbd, "reap_worker_zombies", lambda: None)
        result = kbd.dispatch_once(conn, board="sample", dispatcher_id="worker", spawn_fn=spawn)
        assert [entry[0] for entry in result.spawned] == [task]
    set_dispatch_owner(board, "other")
    assert get_dispatch_owner(board) == "other"


def test_unreadable_metadata_not_erased_by_display_edit(board):
    metadata = board.parent / "board.json"
    metadata.write_text("{broken")
    with pytest.raises(ValueError):
        kb.write_board_metadata("sample", name="Should fail")
    assert metadata.read_text() == "{broken"


def test_unavailable_lock_never_dispatches(board, monkeypatch):
    set_dispatch_owner(board, "worker")
    original = Path.open
    def fail_lock(path, *args, **kwargs):
        if path.name.endswith(".dispatch.lock"):
            raise PermissionError("lock unavailable")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fail_lock)
    with kbc.connect_closing(board) as conn:
        spawn = Mock()
        result = kbd.dispatch_once(conn, board="sample", dispatcher_id="worker", spawn_fn=spawn)
        assert result.skipped_locked
        spawn.assert_not_called()
    with pytest.raises(RuntimeError):
        set_dispatch_owner(board, None)


def test_alias_symlink_uses_actual_owner(board, tmp_path, monkeypatch):
    set_dispatch_owner(board, "worker")
    alias = tmp_path / "alias.db"
    alias.symlink_to(board)
    with kbc.connect_closing(alias) as conn:
        spawn = Mock()
        result = kbd.dispatch_once(conn, board="default", spawn_fn=spawn)
        assert result.skipped_owned
        assert result.dispatch_owner == "worker"
        spawn.assert_not_called()
