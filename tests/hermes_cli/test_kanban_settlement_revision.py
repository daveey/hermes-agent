"""Writer-certified monotonic settlement generations must not suffer value ABA."""
import sqlite3

import pytest

from hermes_cli import kanban_activity_revision as rev


def mark(conn, task='a'):
    return conn.execute('SELECT settlement_rev FROM kanban_task_change WHERE task_id=?', (task,)).fetchone()[0]


@pytest.fixture
def board(tmp_path):
    conn = sqlite3.connect(tmp_path / 'board.db')
    conn.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, status, block_kind, completed_at, title TEXT)')
    conn.execute('CREATE TABLE task_comments (task_id TEXT, body TEXT)')
    rev.ensure_revision(conn, rev.LOCAL_TABLES)
    conn.execute("INSERT INTO tasks VALUES ('a', 'done', NULL, 100, 'title')")
    conn.commit()
    yield conn
    conn.close()


def test_settlement_generation_survives_value_aba_but_ignores_comments(board):
    start = mark(board)
    board.execute("INSERT INTO task_comments VALUES ('a', 'comment')")
    board.execute("UPDATE tasks SET title='changed' WHERE id='a'")
    board.commit()
    assert mark(board) == start
    board.execute("UPDATE tasks SET status='running' WHERE id='a'")
    board.commit()
    middle = mark(board)
    board.execute("UPDATE tasks SET status='done' WHERE id='a'")
    board.commit()
    assert start < middle < mark(board)


def test_certified_settlement_marks_include_tombstones(board):
    from hermes_cli import kanban_task_change as tc
    with board:
        board.execute("DELETE FROM tasks WHERE id='a'")
    board.execute('BEGIN')
    try:
        _, cursor = tc.changed_since(board, None)
        assert tc.settlement_marks(board, {'a', 'missing'}, cursor) == {'a': mark(board)}
    finally:
        board.rollback()


@pytest.mark.parametrize('before,after', [(1, 1.0), ('done', b'done'), (None, ''), ('', None)])
def test_storage_type_and_null_aba_stamp_each_transition(board, before, after):
    board.execute('UPDATE tasks SET completed_at=?', (before,))
    board.commit()
    start = mark(board)
    board.execute('UPDATE tasks SET completed_at=?', (after,))
    middle = mark(board)
    board.execute('UPDATE tasks SET completed_at=?', (before,))
    board.commit()
    assert start < middle < mark(board)
    stable = mark(board)
    board.execute('UPDATE tasks SET completed_at=completed_at, status=status')
    board.commit()
    assert mark(board) == stable


def test_delete_reinsert_rename_and_reinstall_preserve_generations(board):
    before = mark(board)
    board.execute("DELETE FROM tasks WHERE id='a'")
    deleted = mark(board)
    board.execute("INSERT INTO tasks VALUES ('a', 'done', NULL, 100, 'title')")
    inserted = mark(board)
    board.execute("UPDATE tasks SET id='b' WHERE id='a'")
    renamed = mark(board)
    board.commit()
    assert before < deleted < inserted < renamed == mark(board, 'b')
    rev.drop_triggers(board)
    rev.ensure_revision(board, rev.LOCAL_TABLES)
    assert renamed == mark(board) == mark(board, 'b')


def test_upgrade_old_change_table_and_idempotent_init(tmp_path):
    with sqlite3.connect(tmp_path / 'old.db') as conn:
        conn.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)')
        conn.execute('CREATE TABLE kanban_task_change (task_id TEXT PRIMARY KEY NOT NULL, rev INTEGER NOT NULL)')
        conn.execute("INSERT INTO kanban_task_change VALUES ('a', 23)")
        conn.commit()
        rev.ensure_revision(conn, rev.LOCAL_TABLES)
        assert mark(conn) == 0
        before = conn.total_changes
        rev.ensure_revision(conn, rev.LOCAL_TABLES)
        assert conn.total_changes == before
        conn.execute("INSERT INTO tasks VALUES ('a', 'done')")
        assert mark(conn) > 0


@pytest.mark.parametrize('failure', ['old', 'missing', 'column', 'cursor'])
def test_narrow_marks_require_current_writer_coverage(board, failure):
    from dataclasses import replace
    from hermes_cli import kanban_task_change as tc
    if failure in {'old', 'missing'}:
        sql = board.execute("SELECT sql FROM sqlite_master WHERE name='kanban_activity_rev_tasks_update'").fetchone()[0]
        board.execute('DROP TRIGGER kanban_activity_rev_tasks_update')
        if failure == 'old':
            board.execute(sql.replace('kanban-activity-epoch:4', 'kanban-activity-epoch:3'))
    elif failure == 'column':
        rev.drop_triggers(board)
        board.execute('ALTER TABLE kanban_task_change DROP COLUMN settlement_rev')
    board.commit()
    board.execute('BEGIN')
    try:
        _, cursor = tc.changed_since(board, None)
        if cursor is None:
            assert failure in {'missing', 'column'}
            return
        if failure == 'cursor':
            cursor = replace(cursor, revision=cursor.revision + 1)
        assert tc.settlement_marks(board, {'a'}, cursor) is None
    finally:
        board.rollback()


def test_narrow_reader_requires_snapshot(board):
    from hermes_cli import kanban_task_change as tc
    _, cursor = tc.changed_since(board, None)
    with pytest.raises(ValueError, match='caller transaction'):
        tc.settlement_marks(board, {'a'}, cursor)


def test_case_insensitive_columns_still_stamp_exact_value_aba(tmp_path):
    with sqlite3.connect(tmp_path / 'collated.db') as conn:
        conn.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT COLLATE NOCASE)')
        rev.ensure_revision(conn, rev.LOCAL_TABLES)
        conn.execute("INSERT INTO tasks VALUES ('a', 'done')")
        before = mark(conn)
        conn.execute("UPDATE tasks SET status='DONE'")
        middle = mark(conn)
        conn.execute("UPDATE tasks SET status='done'")
        assert before < middle < mark(conn)
