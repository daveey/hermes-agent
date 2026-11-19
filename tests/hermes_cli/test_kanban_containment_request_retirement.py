"""Released sender-request custody retires atomically with protected history."""
from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import kanban_containment_connections as connections
from hermes_cli import kanban_containment_requests as requests
from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_containment_delete_paths as paths
from tests.hermes_cli import test_kanban_containment_remote as rm

board = rm.board


def _rows(conn):
    return [tuple(row) for row in conn.execute("SELECT * FROM containment_delivery ORDER BY request_uid")]


def test_retirement_rolls_back_with_failed_task_delete(board, monkeypatch):
    task = paths._released_child(board, monkeypatch)
    before = _rows(board)
    assert before
    board.execute("CREATE TRIGGER fixture_delete_failure BEFORE DELETE ON tasks "
                  "BEGIN SELECT RAISE(ABORT,'fixture-delete-failure'); END")
    from hermes_cli.kanban_db_archive import archive_task
    assert archive_task(board, task)
    with pytest.raises(sqlite3.IntegrityError, match="fixture-delete-failure"):
        kb.delete_archived_task(board, task)
    assert _rows(board) == before
    assert kb.get_task(board, task) is not None
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='contained'", (task,)).fetchone()[0] == 1
    assert not connections.held(board, task)
    board.execute("DROP TRIGGER fixture_delete_failure")
    assert kb.delete_archived_task(board, task)
    assert not requests.pending(board, task, set())


def test_retirement_filters_exact_request_coordinates(board, monkeypatch):
    task = paths._released_child(board, monkeypatch)
    row = board.execute("SELECT board,gateway,op_json FROM containment_delivery "
                        "WHERE json_extract(op_json,'$.op')='contain'").fetchone()
    base = json.loads(row[2])
    variants = [
        ("foreign-board", "elsewhere", row[1], base),
        ("foreign-gateway", row[0], "elsewhere", base),
        ("foreign-task", row[0], row[1], {**base, "task": "other-task"}),
        ("not-stop", row[0], row[1], {**base, "op": "resume_contained"}),
        ("unknown-stop", row[0], row[1], {**base, "event_uid": "unknown-stop"}),
        ("release-event", row[0], row[1], {**base, "event_uid": board.execute(
            "SELECT uid FROM task_events WHERE task_id=? AND kind='containment_released'", (task,)).fetchone()[0]}),
        ("corrupt", row[0], row[1], "{not-json"),
    ]
    for request, board_id, gateway, op in variants:
        board.execute("INSERT INTO containment_delivery(request_uid,board,gateway,op_json,confirmed) VALUES (?,?,?,?,1)",
                      (request, board_id, gateway, op if isinstance(op, str) else json.dumps(op)))
    # Call the internal cleanup seam directly to isolate each SQL coordinate;
    # actual delete custody must refuse unknown/corrupt requests before this seam.
    with kb.write_txn(board):
        requests.retire_released(board, task)
    remaining = {row[0] for row in board.execute("SELECT request_uid FROM containment_delivery")}
    assert {variant[0] for variant in variants} <= remaining
    assert not board.execute("SELECT 1 FROM containment_delivery WHERE board=? AND gateway=? AND op_json=?",
                             (row[0], row[1], row[2])).fetchone()


@pytest.mark.parametrize("raw", ["{not-json", json.dumps({"op": "contain", "task": "TARGET", "event_uid": "unknown"})])
def test_unresolved_sender_request_blocks_cleanup(board, monkeypatch, raw):
    task = paths._released_child(board, monkeypatch)
    state = board.execute("SELECT board_id,gateway_id FROM kanban_sync_state").fetchone()
    board.execute("INSERT INTO containment_delivery(request_uid,board,gateway,op_json,confirmed) VALUES (?,?,?,?,0)",
                  ("uncertain-request", state[0], state[1], raw.replace("TARGET", task)))
    before = _rows(board)
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        with kb.write_txn(board):
            kb._delete_task_relations(board, task)
    assert _rows(board) == before
    assert connections.held(board, task)
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='contained'", (task,)).fetchone()[0] == 1


def test_delete_scope_outside_transaction_does_not_retire_requests(board, monkeypatch):
    task = paths._released_child(board, monkeypatch)
    before = _rows(board)
    assert not board.in_transaction
    with connections.released_history_delete(board, task):
        assert not board.in_transaction
        assert _rows(board) == before
    assert _rows(board) == before


def test_retirement_without_sync_state_is_inert():
    conn = sqlite3.connect(":memory:")
    try:
        from hermes_cli import kanban_containment_canonical as canonical
        canonical._schema(conn)
        conn.execute("INSERT INTO containment_delivery(request_uid,board,gateway,op_json,confirmed) VALUES (?,?,?,?,1)",
                     ("retained", "fixture", "worker", "{}"))
        requests.retire_released(conn, "task")
        assert conn.execute("SELECT COUNT(*) FROM containment_delivery").fetchone()[0] == 1
    finally:
        conn.close()


def test_retirement_without_delivery_table_is_inert(board, monkeypatch):
    paths._released_child(board, monkeypatch)
    board.execute("DROP TABLE containment_delivery")
    requests.retire_released(board, "task")
    assert not board.execute("SELECT 1 FROM sqlite_master WHERE name='containment_delivery'").fetchone()


def test_eviction_keeps_active_containment(board, monkeypatch, caplog):
    from gateway.session_context import scoped_current_session_id
    from hermes_cli import kanban_containment_service as service
    relay, authority, session, _hub = rm.setup(board, monkeypatch)
    child = paths._child(board, relay)
    rm.adopt(board, monkeypatch, relay, session)
    with scoped_current_session_id(session):
        service.contain(board, task_id=child, authority=authority, summary="Fixture")
    paths._as_foreign_copy(board, child)
    before = _rows(board)
    gone, _failed = paths._evict(board, monkeypatch, caplog)
    assert child not in gone and kb.get_task(board, child) is not None
    assert _rows(board) == before
    assert connections.held(board, child)
