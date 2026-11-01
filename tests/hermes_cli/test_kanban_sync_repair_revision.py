"""Repair invalidation is independent of capture and transactionally covers every input table."""

import sqlite3

import pytest

from hermes_cli import kanban_sync_repair_revision as revision


@pytest.fixture
def board(tmp_path):
    conn = sqlite3.connect(tmp_path / "board.db", isolation_level=None)
    for table in revision.TABLES:
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, value TEXT)")
    revision.ensure_revision(conn)
    yield conn
    conn.close()


def _value(conn):
    return conn.execute("SELECT revision FROM kanban_repair_revision WHERE id = 1").fetchone()[0]


@pytest.mark.parametrize("table", revision.TABLES)
def test_each_input_mutation_invalidates_but_rollback_does_not(board, table):
    before = _value(board)
    board.execute(f"INSERT INTO {table} VALUES (1, 'before')")
    assert _value(board) > before
    before = _value(board)
    board.execute(f"UPDATE {table} SET value = 'after'")
    assert _value(board) > before
    before = _value(board)
    board.execute("BEGIN")
    board.execute(f"DELETE FROM {table}")
    board.rollback()
    assert _value(board) == before
    board.execute(f"DELETE FROM {table}")
    assert _value(board) > before


def test_install_is_read_only_when_intact_and_repairs_missing_trigger(board):
    before = board.total_changes
    revision.ensure_revision(board)
    assert board.total_changes == before
    board.execute("DROP TRIGGER kanban_repair_tasks_update")
    board.execute("INSERT INTO tasks VALUES (1, 'gap')")
    before = _value(board)
    revision.ensure_revision(board)
    assert _value(board) > before
    before = _value(board)
    board.execute("UPDATE tasks SET value = 'observed'")
    assert _value(board) > before


def test_additional_triggers_do_not_cause_perpetual_reinstallation(board):
    board.execute("CREATE TRIGGER kanban_repair_future AFTER INSERT ON tasks BEGIN SELECT 1; END")
    before = board.total_changes
    for _ in range(3):
        revision.ensure_revision(board)
    assert board.total_changes == before


def test_install_recovers_a_removed_revision_table(board):
    board.execute("DROP TABLE kanban_repair_revision")
    revision.ensure_revision(board)
    before = _value(board)
    board.execute("INSERT INTO tasks VALUES (1, 'observed')")
    assert _value(board) > before
