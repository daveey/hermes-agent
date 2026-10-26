"""Existing-task adoption preserves owner state and reuses receipt publication."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli import kanban_origin_creation as creation
from hermes_cli.kanban_origin import OriginError


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    with connect_closing() as conn:
        tid = kb.create_task(conn, title="Existing", session_id="creator")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done',completed_at=123,result='original' WHERE id=?", (tid,))
        yield conn, tid


def intent(tid):
    return {"request_id": "adoption-request-001",
            "owner": {"gateway": "worker", "profile": "default", "board": "default", "task_id": tid},
            "origin": {"gateway": "origin", "profile": "work", "session": "s_old"}}


def test_adopt_done_task_without_creating_or_changing_owner_state(board):
    conn, tid = board
    before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    assert creation.adopt_with_origin(conn, intent(tid), board="default") == tid
    row = conn.execute("SELECT * FROM origin_creation_intents").fetchone()
    marker = json.loads(row["binding_json"])
    assert marker["owner"] == intent(tid)["owner"]
    assert marker["origin"] == intent(tid)["origin"]
    assert marker["task_record_key"] == f"default/default/{tid}"
    assert "publication_version" not in marker
    assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == events
    assert creation.adopt_with_origin(conn, intent(tid), board="default") == tid
    assert dict(conn.execute("SELECT * FROM origin_creation_intents").fetchone()) == dict(row)


@pytest.mark.parametrize("change,code", [
    ({"request_id": "bad"}, "origin_adoption_invalid"),
    ({"origin": {}}, "origin_adoption_invalid"),
    ({"extra": True}, "origin_adoption_invalid"),
])
def test_invalid_adoption_does_not_write(board, change, code):
    conn, tid = board
    with pytest.raises(OriginError) as exc:
        creation.adopt_with_origin(conn, {**intent(tid), **change}, board="default")
    assert exc.value.code == code
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='origin_creation_intents'").fetchone()


def test_exact_task_and_board_required(board):
    conn, tid = board
    for owner, code in [
        ({**intent(tid)["owner"], "board": "other"}, "origin_adoption_invalid"),
        ({**intent(tid)["owner"], "task_id": tid[:-1]}, "origin_task_missing"),
    ]:
        with pytest.raises(OriginError) as exc:
            creation.adopt_with_origin(conn, {**intent(tid), "owner": owner}, board="default")
        assert exc.value.code == code
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='origin_creation_intents'").fetchone()


def test_retry_archive_and_conflicting_request(board):
    conn, tid = board
    creation.adopt_with_origin(conn, intent(tid), board="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (tid,))
    assert creation.adopt_with_origin(conn, intent(tid), board="default") == tid
    changed = {**intent(tid), "origin": {**intent(tid)["origin"], "session": "other"}}
    with pytest.raises(OriginError) as exc:
        creation.adopt_with_origin(conn, changed, board="default")
    assert exc.value.code == "origin_creation_conflict"
    with pytest.raises(OriginError) as exc:
        creation.adopt_with_origin(conn, {**intent(tid), "request_id": "different-request"}, board="default")
    assert exc.value.code == "origin_already_bound"
    assert "adoption-request-001" in str(exc.value)
    assert conn.execute("SELECT COUNT(*) FROM origin_creation_intents").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "archived"


def test_failed_adoption_insert_rolls_back_without_changing_task(board):
    import sqlite3
    conn, tid = board
    before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    def deny(action, table, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == "origin_creation_intents" else sqlite3.SQLITE_OK
    conn.set_authorizer(deny)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            creation.adopt_with_origin(conn, intent(tid), board="default")
    finally:
        conn.set_authorizer(None)
    assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='origin_creation_intents'").fetchone()
    assert creation.adopt_with_origin(conn, intent(tid), board="default") == tid


def test_concurrent_adoption_replay_keeps_one_marker(board):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    conn, tid = board
    barrier = Barrier(2)
    def adopt():
        with connect_closing() as other:
            barrier.wait(timeout=10)
            return creation.adopt_with_origin(other, intent(tid), board="default")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(adopt) for _ in range(2)]
        assert [f.result(timeout=20) for f in futures] == [tid, tid]
    assert conn.execute("SELECT COUNT(*) FROM origin_creation_intents").fetchone()[0] == 1


def test_registered_adoption_tool_requires_trusted_context(board):
    from tools import kanban_tools
    from tools.registry import registry
    conn, tid = board
    result = json.loads(registry.dispatch("kanban_origin_adopt", {"task_id": tid, "board": "default"},
                                         origin_intent=intent(tid)))
    assert result.get("adopted") is True, result
    assert result["created"] is False
    assert result["handoff_complete"] is False
    assert result["ok"] is False
    assert result["origin_creation"]["binding"]["owner"] == intent(tid)["owner"]
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    refused = json.loads(registry.dispatch("kanban_origin_adopt", {"task_id": tid, "board": "default"}))
    assert refused.get("code") == "origin_context_required", refused
