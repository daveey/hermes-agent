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
    # Courier creation requires genuine renewal authority, which exists only on a synced board.
    (tmp_path / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n")
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


def _strip_late_options(conn):
    row = conn.execute("SELECT request_id,intent_json FROM origin_creation_intents").fetchone()
    env = json.loads(row["intent_json"])
    for k in ("chat_origin", "placement_policy", "admission_class", "admission_frees"):
        env["options"].pop(k, None)
    conn.execute("UPDATE origin_creation_intents SET intent_json=? WHERE request_id=?",
                 (json.dumps(env, sort_keys=True, separators=(",", ":")), row["request_id"]))
    conn.commit()


def test_retry_ignores_default_options_added_after_intent_was_stored(board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Build", origin_intent=intent())
        _strip_late_options(conn)
        assert kb.create_task(conn, title="Build", origin_intent=intent()) == tid
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_retry_with_non_default_late_option_still_conflicts(board):
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="Build", origin_intent=intent())
        _strip_late_options(conn)
        with pytest.raises(OriginError) as err:
            kb.create_task(conn, title="Build", origin_intent=intent(), chat_origin="gw/prof/sess")
        assert err.value.code == "origin_creation_conflict"


# [daveey] A placed create (origin courier) used to store NULL: the creator's key was dropped, so a guard
# looking the card up by it (`where idempotency_key='review:pr1305:…'`) never matched.
KEY = "review:pr1305:848af5adae9e29a2ac677f26c732c996997a26b2"


def test_creator_key_is_stored_and_retry_returns_same_card(board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Build", idempotency_key=KEY, origin_intent=intent())
        assert kb.get_task(conn, tid).idempotency_key == KEY
        assert kb.create_task(conn, title="Build", idempotency_key=KEY, origin_intent=intent()) == tid
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_key_held_by_another_card_is_a_typed_refusal_not_an_adoption(board):
    with kbc.connect_closing() as conn:
        local = kb.create_task(conn, title="Local review", idempotency_key=KEY)
        with pytest.raises(OriginError) as err:
            kb.create_task(conn, title="Build", idempotency_key=KEY, origin_intent=intent())
        assert err.value.code == "origin_idempotency_conflict"
        assert local in str(err.value)
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        ledger = conn.execute("SELECT name FROM sqlite_master WHERE name='origin_creation_intents'").fetchone()
        assert not ledger or conn.execute("SELECT COUNT(*) FROM origin_creation_intents").fetchone()[0] == 0


def test_key_held_only_by_an_archived_card_is_reused(board):
    with kbc.connect_closing() as conn:
        old = kb.create_task(conn, title="Old", idempotency_key=KEY)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (old,))
        tid = kb.create_task(conn, title="Build", idempotency_key=KEY, origin_intent=intent())
        assert tid != old
        assert kb.get_task(conn, tid).idempotency_key == KEY


def test_retry_with_capacity_class_after_pre_admission_intent_conflicts(board):
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="Build", origin_intent=intent())
        _strip_late_options(conn)
        with pytest.raises(OriginError) as err:
            kb.create_task(conn, title="Build", origin_intent=intent(), admission_class="capacity",
                           admission_frees="frees: worker slots")
        assert err.value.code == "origin_creation_conflict"


def test_courier_create_keeps_capacity_class_and_retry_matches(board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Build", origin_intent=intent(), admission_class="capacity",
                             admission_frees="frees: worker slots")
        task = kb.get_task(conn, tid)
        assert (task.admission_class, task.admission_frees) == ("capacity", "frees: worker slots")
        assert kb.create_task(conn, title="Build", origin_intent=intent(), admission_class="capacity",
                              admission_frees="frees: worker slots") == tid
