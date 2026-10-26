"""Lifecycle refusal and actionable operator errors under dispatch contention."""
import argparse

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_boards import _dispatch_boards


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    return tmp_path


@pytest.mark.parametrize("archive", [True, False])
def test_missing_board_removal_creates_nothing(home, archive):
    with pytest.raises(ValueError, match="does not exist"):
        kb.remove_board("missing", archive=archive)
    assert not kb.board_dir("missing").exists()
    assert not (kb.boards_root() / "_archived").exists()
    assert not kb.board_exists("missing")


@pytest.mark.parametrize("action", ["create", "rename", "set-default-workdir"])
def test_cli_busy_metadata_is_actionable_error(home, capsys, action):
    kb.create_board("sample")
    path = kb.board_dir("sample") / "kanban.db"
    args = argparse.Namespace(boards_action=action, slug="sample", name="New", path=str(home),
                              description=None, icon=None, color=None, default_workdir=None,
                              project=None, switch=False, json=False)
    with kbc._dispatch_tick_lock(path) as held:
        assert held
        assert _dispatch_boards(args) != 0
    assert "busy" in capsys.readouterr().err
    assert kb.read_board_metadata("sample")["name"] != "New"
