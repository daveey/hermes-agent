"""A kanban tool whose board raises a SQLite error must name the safe fallback (self-heal agent-6eb3a51e94).

On 2026-10-27 five kanban_* calls returned a bare ``kanban_show: disk I/O error``. With no next step the agent
wrote the live board with raw ``sqlite3`` (INSERT/UPDATE) and deleted its ``-shm`` and ``.corrupt.*.bak``
files. The error now carries a ``hint``: retry once, then ``hermes kanban <verb>`` in a fresh process, never
raw sqlite3 writes or deleting the board's files, ``hermes kanban recover`` when the integrity check fails,
block when the CLI fails too.
"""
import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def board_raises(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    from tools import kanban_tools

    def install(exc):
        def _broken(*_a, **_k):
            raise exc
        monkeypatch.setattr(kanban_tools, "_board", _broken)
    return install


def _dispatch(name, args):
    from tools import kanban_tools  # registers the handlers
    from tools.registry import registry
    return json.loads(registry.dispatch(name, args))


def _assert_safe_hint(result):
    hint = result.get("hint") or ""
    assert hint, result
    assert "hermes kanban" in hint
    assert "hermes kanban recover" in hint
    assert "sqlite3" in hint and "never" in hint.lower()
    assert "-shm" in hint and ".corrupt" in hint
    assert "kanban_block" in hint or "block" in hint.lower()


def test_disk_io_error_on_show_carries_the_cli_fallback_hint(board_raises):
    board_raises(sqlite3.OperationalError("disk I/O error"))
    result = _dispatch("kanban_show", {"task_id": "t_x"})
    assert "disk I/O error" in result["error"]
    _assert_safe_hint(result)
    assert "hermes kanban show t_x" in result["hint"]


def test_disk_io_error_on_comment_names_the_comment_verb(board_raises):
    board_raises(sqlite3.OperationalError("disk I/O error"))
    result = _dispatch("kanban_comment", {"task_id": "t_x", "body": "note"})
    _assert_safe_hint(result)
    assert "hermes kanban comment" in result["hint"]


def test_database_error_and_corrupt_board_carry_the_hint(board_raises):
    board_raises(sqlite3.DatabaseError("database disk image is malformed"))
    _assert_safe_hint(_dispatch("kanban_show", {"task_id": "t_x"}))
    from hermes_cli.kanban_db_connect import KanbanDbCorruptError
    board_raises(KanbanDbCorruptError(Path("/x/kanban.db"), None, "integrity_check failed"))
    _assert_safe_hint(_dispatch("kanban_list", {}))


def test_named_board_hint_puts_the_board_flag_before_the_verb(board_raises):
    board_raises(sqlite3.OperationalError("disk I/O error"))
    result = _dispatch("kanban_show", {"task_id": "t_x", "board": "ops"})
    assert "hermes kanban --board ops show t_x" in result["hint"]
    assert "hermes kanban --board ops recover" in result["hint"]


def test_other_errors_get_no_db_hint(board_raises):
    board_raises(ValueError("unknown board 'nope'"))
    result = _dispatch("kanban_show", {"task_id": "t_x"})
    assert "hint" not in result, result
