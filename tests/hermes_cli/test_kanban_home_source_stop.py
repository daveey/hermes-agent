"""Source STOP changes home, not evidence, probe custody or execution authority."""
from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from hermes_cli import kanban_home_transfer as transfer
from tests.hermes_cli import test_kanban_home_transfer_fence as support

board = support.board


def record(conn, tid) -> dict:
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    fields = {sync._wire_name("tasks", col): row[col] for col in sync.captured_columns(conn, "tasks")}
    fields.update(id=tid, uid=tid, created_on="hub", version=8, deleted_at=None)
    return dict(task=tid, source="worker-a3", destination="hub", nonce="source-stop-000001",
                expected_version=7, phase="materialized", rows=[dict(table="tasks", uid=tid, fields=fields)])


def prepared(conn):
    tid = support.seed(conn)
    kb.add_comment(conn, tid, "actor", "retained history")
    with kbc.write_txn(conn):
        conn.execute("INSERT INTO task_wake_checks(task_id,cmd,every,next_at) VALUES (?,'source-only-probe',120,1)", (tid,))
        conn.execute("DELETE FROM kanban_sync_outbox")
        reply = record(conn, tid)
        transfer.prepare(conn, tid, source=reply["source"], destination=reply["destination"],
                         nonce=reply["nonce"], expected_version=reply["expected_version"])
    return tid, reply


def test_source_stop_installs_epoch_and_changes_home_without_erasing_probe_history(board):
    tid, reply = prepared(board)
    before = tuple(board.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    children = [tuple(r) for r in board.execute("SELECT * FROM task_comments WHERE task_id=?", (tid,))]
    probes = [tuple(r) for r in board.execute("SELECT * FROM task_wake_checks WHERE task_id=?", (tid,))]
    with kbc.write_txn(board):
        assert transfer.finalize_source(board, reply) == dict(task=tid,nonce=reply["nonce"],phase="stopped_local")
    row = board.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    names = [r[1] for r in board.execute("PRAGMA table_info(tasks)")]
    changed = {k for k, a, b in zip(names, before, tuple(row)) if a != b}
    assert changed == {"created_on", "sync_version"}
    assert row["created_on"] == "hub" and row["sync_version"] == 8
    assert [tuple(r) for r in board.execute("SELECT * FROM task_comments WHERE task_id=?", (tid,))] == children
    assert [tuple(r) for r in board.execute("SELECT * FROM task_wake_checks WHERE task_id=?", (tid,))] == probes
    assert board.execute("SELECT count(*) FROM kanban_sync_outbox").fetchone()[0] == 0
    assert board.execute("SELECT nonce FROM kanban_home_generations1 WHERE task_id=?", (tid,)).fetchone()[0] == reply["nonce"]
    # Retained foreign probe custody can write a wake later; capture has the new
    # epoch, not permission to execute without the usual Board lease/hold gates.
    kb.add_comment(board, tid, "actor", "fresh post-stop evidence")
    captures = board.execute("SELECT home_generations FROM kanban_sync_outbox WHERE table_name='task_comments'").fetchall()
    assert captures and all(json.loads(r[0]) == {tid:reply["nonce"]} for r in captures)


def test_retry_survives_reopen_and_never_rewinds_later_native_state(board):
    tid, reply = prepared(board)
    with kbc.write_txn(board):
        transfer.finalize_source(board, reply)
        board.execute("UPDATE tasks SET title='later real work',sync_version=9 WHERE id=?", (tid,))
    path = board.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(path) as other:
        other.row_factory = sqlite3.Row
        other.execute("BEGIN IMMEDIATE")
        assert transfer.finalize_source(other, {**reply,"phase":"complete"})["phase"] == "stopped_local"
        other.commit()
        assert other.execute("SELECT title,sync_version FROM tasks WHERE id=?", (tid,)).fetchone()[:] == ("later real work",9)


@pytest.mark.parametrize("change", [
    {"phase":"committed"}, {"nonce":"another-stop-000001"}, {"expected_version":True},
    {"source":"worker-a2"}, {"destination":"worker-a2"}, {"rows":[]},
])
def test_nonmatching_or_unacknowledged_stop_keeps_the_source_fenced(board, change):
    tid, reply = prepared(board)
    with kbc.write_txn(board):
        with pytest.raises(ValueError):
            transfer.finalize_source(board, {**reply,**change})
    with pytest.raises(sqlite3.IntegrityError,match="home transfer prepared"):
        with kbc.write_txn(board):
            board.execute("UPDATE tasks SET title='must stay fenced' WHERE id=?", (tid,))
    assert board.execute("SELECT count(*) FROM kanban_home_finalized1").fetchone()[0] == 0


def test_same_tuple_but_changed_business_fields_is_not_home_only(board):
    tid, reply = prepared(board)
    bad = copy.deepcopy(reply)
    bad["rows"][0]["fields"]["status"] = "done"
    with kbc.write_txn(board):
        with pytest.raises(ValueError,match="changed task field: status"):
            transfer.finalize_source(board,bad)
    assert board.execute("SELECT created_on FROM tasks WHERE id=?",(tid,)).fetchone()[0] == "worker-a3"


def test_crash_after_receipt_before_home_update_rolls_back_epoch_and_release(board):
    tid, reply = prepared(board)
    with kbc.write_txn(board):
        board.execute("CREATE TRIGGER fail_home_stop BEFORE UPDATE OF created_on ON tasks "
                      "BEGIN SELECT RAISE(ABORT,'injected stop failure'); END")
        with pytest.raises(sqlite3.IntegrityError,match="injected stop failure"):
            transfer.finalize_source(board,reply)
    assert board.execute("SELECT count(*) FROM kanban_home_finalized1").fetchone()[0] == 0
    assert board.execute("SELECT count(*) FROM kanban_home_generations1 WHERE task_id=?",(tid,)).fetchone()[0] == 0
    assert board.execute("SELECT created_on FROM tasks WHERE id=?",(tid,)).fetchone()[0] == "worker-a3"
    with pytest.raises(sqlite3.IntegrityError,match="home transfer prepared"):
        with kbc.write_txn(board):
            board.execute("UPDATE tasks SET title='still fenced' WHERE id=?",(tid,))
    with kbc.write_txn(board):
        board.execute("DROP TRIGGER fail_home_stop")
        transfer.finalize_source(board,reply)


@pytest.mark.parametrize("raw", ["not-json", "{}", '{"phase":"materialized","rows":[]}'])
def test_malformed_receipt_cannot_release_a_matching_nonce(board, raw):
    tid, reply = prepared(board)
    with pytest.raises(sqlite3.Error):
        with kbc.write_txn(board):
            board.execute("INSERT INTO kanban_home_finalized1(task_id,nonce,receipt) VALUES (?,?,?)",
                          (tid,reply["nonce"],raw))
    with pytest.raises(sqlite3.IntegrityError,match="home transfer prepared"):
        with kbc.write_txn(board):
            board.execute("UPDATE tasks SET title='still prepared' WHERE id=?",(tid,))


def test_source_stop_is_atomically_visible_to_an_existing_peer(board):
    tid, reply = prepared(board)
    path = board.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(f"file:{path}?mode=ro",uri=True) as peer:
        with kbc.write_txn(board):
            transfer.finalize_source(board,reply)
            assert peer.execute("SELECT created_on FROM tasks WHERE id=?",(tid,)).fetchone()[0] == "worker-a3"
            assert peer.execute("SELECT count(*) FROM kanban_home_finalized1").fetchone()[0] == 0
            assert peer.execute("SELECT count(*) FROM kanban_home_generations1 WHERE task_id=?",(tid,)).fetchone()[0] == 0
        assert peer.execute("SELECT created_on FROM tasks WHERE id=?",(tid,)).fetchone()[0] == "hub"
        assert peer.execute("SELECT nonce FROM kanban_home_generations1 WHERE task_id=?",(tid,)).fetchone()[0] == reply["nonce"]
        assert peer.execute("SELECT nonce FROM kanban_home_finalized1 WHERE task_id=?",(tid,)).fetchone()[0] == reply["nonce"]


@pytest.mark.parametrize("sql", ["DELETE FROM kanban_home_finalized1 WHERE task_id=?",
    "UPDATE kanban_home_finalized1 SET nonce='another-epoch' WHERE task_id=?",
    "INSERT OR REPLACE INTO kanban_home_finalized1 SELECT * FROM kanban_home_finalized1 WHERE task_id=?"])
def test_completed_custody_receipt_is_immutable(board,sql):
    tid,reply=prepared(board)
    with kbc.write_txn(board):
        transfer.finalize_source(board,reply)
    with pytest.raises(sqlite3.IntegrityError,match="finalization immutable"):
        with kbc.write_txn(board):
            board.execute(sql,(tid,))
