"""Local live-home preparation fences SQLite writers, not just dispatcher ticks."""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    yield from support.synced_conn(home)


def prepare(conn, tid, nonce="transfer-00000001"):
    from hermes_cli import kanban_home_transfer

    return kanban_home_transfer.prepare(conn, tid, source="worker-a3", destination="hermes-hub",
                                       nonce=nonce, expected_version=7)


def seed(conn):
    tid = kb.create_task(conn, title="existing worker home")
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET created_on='worker-a3',sync_version=7 WHERE id=?", (tid,))
        conn.execute("UPDATE kanban_sync_state SET gateway_id='worker-a3'")
        conn.execute("DELETE FROM kanban_sync_outbox")
    return tid


def test_prepare_stops_an_existing_connection_even_with_sync_applying(board):
    tid = seed(board)
    before = board.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    with kbc.write_txn(board):
        prepare(board, tid)
    # applying is only an echo guard, never administrative-transfer permission.
    with kbc.write_txn(board):
        board.execute("UPDATE kanban_sync_state SET applying=1")
    with pytest.raises(sqlite3.IntegrityError, match="home transfer prepared"):
        with kbc.write_txn(board):
            board.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
    assert board.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == before


@pytest.mark.parametrize("sql", [
    "UPDATE tasks SET claim_lock='other:123' WHERE id=?",
    "UPDATE tasks SET id='replacement-task' WHERE id=?",
    "DELETE FROM tasks WHERE id=?",
    "INSERT INTO task_comments(task_id,author,body,created_at) VALUES (?,'actor','late',1)",
    "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,'late','{}',1)",
    "INSERT INTO task_runs(task_id,profile,status,started_at) VALUES (?,'default','running',1)",
    "INSERT INTO task_links(parent_id,child_id) VALUES (?,'missing-child')",
    "INSERT INTO task_links(child_id,parent_id) VALUES (?,'missing-parent')",
])
def test_task_children_and_both_link_directions_are_fenced(board, sql):
    tid = seed(board)
    with kbc.write_txn(board):
        prepare(board, tid)
    with pytest.raises(sqlite3.IntegrityError, match="home transfer prepared"):
        with kbc.write_txn(board):
            board.execute(sql, (tid,))


def test_prepare_is_durable_idempotent_and_does_not_block_a_neighbour(board):
    tid = seed(board)
    with kbc.write_txn(board):
        first = prepare(board, tid)
    with kbc.write_txn(board):
        assert prepare(board, tid) == first
    with pytest.raises(ValueError, match="identity changed"):
        with kbc.write_txn(board):
            prepare(board, tid, nonce="transfer-00000002")
    other = kb.create_task(board, title="unrelated task")
    kb.add_comment(board, other, "actor", "still works")
    assert first["phase"] == "prepared_local"
    assert board.execute("SELECT count(*) FROM task_comments WHERE task_id=?", (other,)).fetchone()[0] == 1


@pytest.mark.parametrize("mutation, message", [
    ("UPDATE tasks SET sync_version=8 WHERE id=?", "version or source changed"),
    ("UPDATE tasks SET created_on='worker-a2' WHERE id=?", "version or source changed"),
    ("UPDATE tasks SET status='running' WHERE id=?", "idle open task"),
    ("UPDATE tasks SET worker_pid=123 WHERE id=?", "idle open task"),
    ("UPDATE tasks SET claim_lock='actor:123' WHERE id=?", "idle open task"),
    ("UPDATE tasks SET lease_holder='peer' WHERE id=?", "idle open task"),
    ("UPDATE tasks SET workspace_custody='{}' WHERE id=?", "idle open task"),
    ("INSERT INTO task_runs(task_id,profile,status,started_at) VALUES (?,'default','running',1)", "unended run"),
])
def test_refusal_rolls_back_even_when_outer_caller_commits(board, mutation, message):
    tid = seed(board)
    with kbc.write_txn(board):
        board.execute(mutation, (tid,))
        board.execute("DELETE FROM kanban_sync_outbox")
    with kbc.write_txn(board):
        with pytest.raises(ValueError, match=message):
            prepare(board, tid)
    assert board.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_home_prepared1'").fetchone() is None


def test_replacement_of_a_child_cannot_delete_a_fenced_old_identity(board):
    tid = seed(board)
    other = kb.create_task(board, title="unfenced neighbour")
    kb.add_comment(board, tid, "actor", "retained")
    with kbc.write_txn(board):
        board.execute("DELETE FROM kanban_sync_outbox")
        row = board.execute("SELECT id,uid FROM task_comments WHERE task_id=?", (tid,)).fetchone()
        prepare(board, tid)
    for col, value in (("id", row[0]), ("uid", row[1])):
        with pytest.raises(sqlite3.IntegrityError, match="home transfer prepared"):
            with kbc.write_txn(board):
                board.execute(f"INSERT OR REPLACE INTO task_comments({col},task_id,author,body,created_at) "
                              "VALUES (?,?,'actor','wrong home',1)", (value, other))
    assert board.execute("SELECT body FROM task_comments WHERE id=?", (row[0],)).fetchone()[0] == "retained"


def test_pending_custody_is_not_discarded_to_make_preparation_pass(board):
    tid = seed(board)
    kb.add_comment(board, tid, "actor", "not pushed yet")
    before = board.execute("SELECT count(*) FROM kanban_sync_outbox").fetchone()[0]
    with kbc.write_txn(board):
        with pytest.raises(ValueError, match="drained pending custody"):
            prepare(board, tid)
    assert board.execute("SELECT count(*) FROM kanban_sync_outbox").fetchone()[0] == before


def test_probe_custody_cannot_change_while_prepared(board):
    tid = seed(board)
    with kbc.write_txn(board):
        board.execute("INSERT INTO task_wake_checks(task_id,cmd,every,next_at) VALUES (?,'true',120,1)", (tid,))
        prepare(board, tid)
    with pytest.raises(sqlite3.IntegrityError, match="home transfer prepared"):
        with kbc.write_txn(board):
            board.execute("DELETE FROM task_wake_checks WHERE task_id=?", (tid,))


def test_fence_survives_reopen_and_refuses_sync_detach_evidence_loss(board):
    from hermes_cli import kanban_db_sync as sync

    tid = seed(board)
    path = board.execute("PRAGMA database_list").fetchone()[2]
    with kbc.write_txn(board):
        prepare(board, tid)
    # Normal detach clears captured origin custody, so it must fail closed too.
    # Its refusal must not erase the independent administrative-home fence.
    with pytest.raises(sqlite3.IntegrityError, match="home transfer prepared"):
        with kbc.write_txn(board):
            sync.drop_triggers(board)
    # Raw old connections do not have to import this module to be fenced.
    with sqlite3.connect(path) as other:
        with pytest.raises(sqlite3.IntegrityError, match="home transfer prepared"):
            other.execute("UPDATE tasks SET title='stale writer' WHERE id=?", (tid,))
    assert board.execute("SELECT title FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "existing worker home"


@pytest.mark.parametrize("sql", [
    "DELETE FROM kanban_home_prepared1 WHERE task_id=?",
    "UPDATE kanban_home_prepared1 SET nonce='another-nonce-0001' WHERE task_id=?",
    "INSERT OR REPLACE INTO kanban_home_prepared1 SELECT * FROM kanban_home_prepared1 WHERE task_id=?",
])
def test_uncertain_preparation_cannot_be_reset_or_replaced(board, sql):
    tid = seed(board)
    with kbc.write_txn(board):
        prepare(board, tid)
    with pytest.raises(sqlite3.IntegrityError, match="preparation immutable"):
        with kbc.write_txn(board):
            board.execute(sql, (tid,))


def test_private_custody_primary_key_replacement_checks_the_old_task(board):
    tid = seed(board)
    other = kb.create_task(board, title="unprepared")
    with kbc.write_txn(board):
        board.execute("CREATE TABLE transfer_fixture_custody(episode TEXT PRIMARY KEY,task_id TEXT NOT NULL)")
        board.execute("INSERT INTO transfer_fixture_custody VALUES ('episode-1',?)", (tid,))
        board.execute("DELETE FROM kanban_sync_outbox")
        prepare(board, tid)
    with pytest.raises(sqlite3.IntegrityError, match="home transfer prepared"):
        with kbc.write_txn(board):
            board.execute("INSERT OR REPLACE INTO transfer_fixture_custody VALUES ('episode-1',?)", (other,))
    assert board.execute("SELECT task_id FROM transfer_fixture_custody").fetchone()[0] == tid
