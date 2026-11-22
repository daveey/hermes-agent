"""Real SQLite downgrade fences; activation is fixture-only. [daveey]"""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import kanban_sync_support as support
from hermes_cli import kanban_db_sync as sync


def gate():
    import importlib.util
    assert importlib.util.find_spec("hermes_cli.kanban_sync_writer_gate") is not None, "DB writer gate is missing"
    from hermes_cli import kanban_sync_writer_gate
    return kanban_sync_writer_gate


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    path = home / "kanban.db"
    conn = kbc.connect(path)
    try:
        yield path, conn
    finally:
        conn.close()


def test_preopened_legacy_writer_is_refused_after_activation(board):
    path, conn = board
    old = sqlite3.connect(path, isolation_level=None)  # noqa: kanban-lockguard -- fixture old-core connection
    try:
        assert old.execute("SELECT count(*) FROM tasks").fetchone() == (0,)
        with kb.write_txn(conn):
            gate().activate(conn)
        with pytest.raises(sqlite3.OperationalError, match="replay_writer_revision"):
            old.execute("INSERT INTO tasks(id,title,status,created_at) VALUES ('t_old','old','todo',1)")
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        assert old.execute("SELECT minimum FROM replay_writer_gate1").fetchone() == (1,)
        conn.execute("INSERT INTO tasks(id,title,status,created_at) VALUES ('t_new','new','todo',1)")
        assert conn.execute("SELECT count(*) FROM kanban_sync_outbox").fetchone()[0] == 1
    finally:
        old.close()


def test_gate_minimum_cannot_be_replaced_or_removed(board):
    _, conn = board
    with kb.write_txn(conn):
        gate().activate(conn)
    conn.execute("UPDATE replay_writer_gate1 SET minimum=2")
    conn.create_function("replay_writer_revision", 0, lambda: 2)
    with pytest.raises(sqlite3.IntegrityError, match="minimum cannot decrease"):
        conn.execute("UPDATE replay_writer_gate1 SET minimum=1")
    with pytest.raises(sqlite3.IntegrityError, match="minimum cannot be removed"):
        conn.execute("DELETE FROM replay_writer_gate1")
    with pytest.raises(sqlite3.IntegrityError, match="minimum cannot be replaced"):
        conn.execute("INSERT OR REPLACE INTO replay_writer_gate1 VALUES(1,1)")
    assert tuple(conn.execute("SELECT * FROM replay_writer_gate1").fetchone()) == (1, 2)


def test_reactivation_preserves_higher_minimum(board):
    _, conn = board
    with kb.write_txn(conn):
        gate().activate(conn)
    conn.execute("UPDATE replay_writer_gate1 SET minimum=2")
    conn.create_function("replay_writer_revision", 0, lambda: 2)
    before = list(conn.iterdump())
    with kb.write_txn(conn):
        gate().activate(conn)
    assert list(conn.iterdump()) == before
    assert conn.execute("SELECT minimum FROM replay_writer_gate1").fetchone()[0] == 2


def test_install_does_not_activate_or_certify(board):
    path, conn = board
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='replay_writer_gate1'").fetchone() is None
    old = sqlite3.connect(path, isolation_level=None)  # noqa: kanban-lockguard -- inactive old-core fixture
    try:
        old.execute("INSERT INTO tasks(id,title,status,created_at) VALUES ('t_old','old','todo',1)")
        row = conn.execute("SELECT capture_protocol,identity_state FROM kanban_sync_outbox").fetchone()
        assert tuple(row) == (None, None)
    finally:
        old.close()


@pytest.mark.parametrize("sql", [
    "INSERT INTO kanban_sync_parent_envelopes VALUES ('t_parent','{}',NULL,NULL,NULL,NULL)",
    "UPDATE kanban_sync_outbox SET op_id='replaced'",
    "DELETE FROM kanban_sync_outbox",
    "UPDATE kanban_sync_state SET applying=1",
    "INSERT INTO kanban_sync_tombstones VALUES ('tasks','t_deleted',1)",
    "UPDATE tasks SET title='lost'",
    "DELETE FROM tasks",
])
def test_old_writer_cannot_mutate_custody_or_business_state(board, sql):
    path, conn = board
    kb.create_task(conn, title="retained", assignee="worker")
    with kb.write_txn(conn):
        gate().activate(conn)
    old = sqlite3.connect(path, isolation_level=None)  # noqa: kanban-lockguard -- unsupported raw writer
    before = list(conn.iterdump())
    try:
        with pytest.raises(sqlite3.OperationalError, match="replay_writer_revision"):
            old.execute(sql)
        assert list(conn.iterdump()) == before
    finally:
        old.close()


def test_sync_detach_and_reinstall_cannot_remove_gate(board):
    path, conn = board
    with kb.write_txn(conn):
        gate().activate(conn)
    with kb.write_txn(conn):
        sync.drop_triggers(conn)
        sync.install_triggers(conn)
    sync.set_mode(conn, "off")
    assert conn.execute("SELECT minimum FROM replay_writer_gate1").fetchone()[0] == 1
    old = sqlite3.connect(path, isolation_level=None)  # noqa: kanban-lockguard -- detached old writer
    try:
        with pytest.raises(sqlite3.OperationalError, match="replay_writer_revision"):
            old.execute("INSERT INTO tasks(id,title,status,created_at) VALUES ('t_old','old','todo',1)")
    finally:
        old.close()
    reopened = kbc.connect(path)
    try:
        kb.create_task(reopened, title="compatible detached writer", assignee="worker")
    finally:
        reopened.close()


def test_gate_activation_rolls_back_with_real_ddl_failure(board):
    path, conn = board
    before = list(conn.iterdump())
    def authorize(action, name, _arg, _db, _source):
        if action == sqlite3.SQLITE_CREATE_TRIGGER and name == 'replay_writer1_tasks_delete':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(authorize)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            with kb.write_txn(conn):
                gate().activate(conn)
    finally:
        conn.set_authorizer(None)
    assert list(conn.iterdump()) == before
    old = sqlite3.connect(path, isolation_level=None)  # noqa: kanban-lockguard -- rollback compatibility
    try:
        old.execute("INSERT INTO tasks(id,title,status,created_at) VALUES ('t_old','old','todo',1)")
    finally:
        old.close()


def test_gate_idempotence_cold_reopen_and_lower_revision(board):
    path, conn = board
    task = kb.create_task(conn, title="retained", assignee="worker")
    with kb.write_txn(conn):
        gate().activate(conn)
    before = list(conn.iterdump())
    with kb.write_txn(conn):
        gate().activate(conn)
    assert list(conn.iterdump()) == before
    restarted = kbc.connect(path)
    try:
        restarted.create_function("replay_writer_revision", 0, lambda: 0)
        with pytest.raises(sqlite3.IntegrityError, match="revision too old"):
            restarted.execute("UPDATE tasks SET title='bad' WHERE id=?", (task,))
        gate().register(restarted)
        restarted.execute("UPDATE tasks SET title='good' WHERE id=?", (task,))
        assert restarted.execute("SELECT title FROM tasks WHERE id=?", (task,)).fetchone()[0] == 'good'
    finally:
        restarted.close()


def test_activation_failure_cannot_be_committed_by_outer_caller(board):
    _, conn = board
    before = list(conn.iterdump())
    def authorize(action, name, _arg, _db, _source):
        return (sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TRIGGER
                and name == 'replay_writer1_tasks_delete' else sqlite3.SQLITE_OK)
    conn.set_authorizer(authorize)
    try:
        with kb.write_txn(conn):
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                gate().activate(conn)
    finally:
        conn.set_authorizer(None)
    assert list(conn.iterdump()) == before


def test_activation_requires_outer_transaction(board):
    _, conn = board
    before = list(conn.iterdump())
    with pytest.raises(ValueError, match="requires a write transaction"):
        gate().activate(conn)
    assert list(conn.iterdump()) == before


def test_every_existing_table_has_all_three_guards(board):
    _, conn = board
    with kb.write_txn(conn):
        gate().activate(conn)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    triggers = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {f'replay_writer1_{table}_{action}' for table in tables
            for action in ('insert', 'update', 'delete')} <= triggers


def test_outer_rollback_discards_successful_activation(board):
    _, conn = board
    before = list(conn.iterdump())
    with pytest.raises(RuntimeError, match="rollback fixture"):
        with kb.write_txn(conn):
            gate().activate(conn)
            raise RuntimeError("rollback fixture")
    assert list(conn.iterdump()) == before


def test_cached_statement_cannot_bypass_new_gate(board):
    path, conn = board
    task = kb.create_task(conn, title="before", assignee="worker")
    old = sqlite3.connect(path, isolation_level=None)  # noqa: kanban-lockguard -- prepared old statement
    sql = "UPDATE tasks SET title=? WHERE id=?"
    try:
        old.execute(sql, ('before', task))
        with kb.write_txn(conn):
            gate().activate(conn)
        with pytest.raises(sqlite3.OperationalError, match="replay_writer_revision"):
            old.execute(sql, ('after', task))
        assert conn.execute("SELECT title FROM tasks WHERE id=?", (task,)).fetchone()[0] == 'before'
    finally:
        old.close()
