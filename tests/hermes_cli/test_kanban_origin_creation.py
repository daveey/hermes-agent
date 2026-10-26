"""Owner creation must durably retain its receipt intent with the actual card."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from hermes_cli.kanban_origin import OriginError

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    return tmp_path


def intent():
    return {"request_id": "request-create-001",
            "owner": {"gateway": "worker", "profile": "default", "board": "default"},
            "origin": {"gateway": "origin", "profile": "work", "session": "s_old"}}


def test_creation_commits_task_and_immutable_publication_marker_together(board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Build", session_id="creator", origin_intent=intent())
        row = conn.execute("SELECT task_id,binding_json,child_request_id FROM origin_creation_intents").fetchone()
        assert row["task_id"] == tid
        marker = json.loads(row["binding_json"])
        assert marker["owner"] == {**intent()["owner"], "task_id": tid}
        assert marker["origin"] == intent()["origin"]
        assert marker["task_record_key"] == f"default/default/{tid}"
        assert marker["version"] == 1
        assert "publication_version" not in marker
        assert row["child_request_id"]
        assert kb.get_task(conn, tid).session_id == "creator"
    with kbc.connect_closing() as conn:
        assert kb.create_task(conn, title="Build", session_id="creator", origin_intent=intent()) == tid
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT child_request_id FROM origin_creation_intents").fetchone()[0] == row["child_request_id"]


@pytest.mark.parametrize("change", [
    {"request_id": ""}, {"request_id": None}, {"extra": True},
    {"owner": {"gateway": "worker", "profile": "../work", "board": "default"}},
    {"origin": {"gateway": "origin", "profile": "work"}},
    {"owner": {"gateway": "worker", "profile": "default", "board": "different"}},
])
def test_invalid_identity_fails_typed_before_creation(board, change):
    with kbc.connect_closing() as conn:
        with pytest.raises(OriginError) as err:
            kb.create_task(conn, title="Build", origin_intent={**intent(), **change})
        assert err.value.code == "origin_creation_invalid"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_archived_retry_and_changed_options(board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Build", origin_intent=intent())
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (tid,))
        assert kb.create_task(conn, title="Build", origin_intent=intent()) == tid
        for changes in ({"title": "Different"}, {"session_id": "another"}, {"priority": 1}):
            with pytest.raises(OriginError) as err:
                kb.create_task(conn, **{"title": "Build", **changes}, origin_intent=intent())
            assert err.value.code == "origin_creation_conflict"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_parallel_retry_creates_one_card(board):
    barrier = Barrier(2)
    def create():
        with kbc.connect_closing() as conn:
            barrier.wait(timeout=10)
            return kb.create_task(conn, title="Build", origin_intent=intent())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create) for _ in range(2)]
        ids = [f.result(timeout=20) for f in futures]
    assert ids[0] == ids[1]
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_named_current_board_is_part_of_intent(board):
    kb.create_board("work")
    kb.set_current_board("work")
    routed = {**intent(), "owner": {**intent()["owner"], "board": "work"}}
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Build", origin_intent=routed)
        marker = json.loads(conn.execute("SELECT binding_json FROM origin_creation_intents").fetchone()[0])
        assert marker["task_record_key"] == f"default/work/{tid}"
        with pytest.raises(OriginError) as err:
            kb.create_task(conn, title="Build", origin_intent=intent())
        assert err.value.code == "origin_creation_invalid"


def test_failed_intent_insert_rolls_back_task_and_events(board):
    with kbc.connect_closing() as conn:
        # Authorizer denies the final ledger INSERT, after nested create returns.
        import sqlite3
        def deny_insert(action, table, *args):
            if action == sqlite3.SQLITE_INSERT and table == "origin_creation_intents":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(deny_insert)
        with pytest.raises(sqlite3.DatabaseError):
            kb.create_task(conn, title="Build", origin_intent=intent())
        conn.set_authorizer(None)
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0
        tid = kb.create_task(conn, title="Build", origin_intent=intent())
        assert kb.get_task(conn, tid).title == "Build"
