"""Real replica freshness/qualification and in-transaction link races."""
import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pr_acceptance_store as acceptance
from tests.hermes_cli import test_kanban_completion_receipt as fixtures

conn = fixtures.conn


@pytest.fixture
def replica(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path))
    db = sqlite3.connect(tmp_path / "hub.db")
    db.execute("CREATE TABLE replica(gw TEXT, coll TEXT, key TEXT, data TEXT, updated_at INTEGER)")
    try:
        yield db
    finally:
        db.close()


def publish(replica, *, age=0, gateway="away", board="default", status="running"):
    task = {"id": "t_owner", "board": board, "gateway": gateway, "profile": "default",
            "owner_profile": "default", "status": status}
    scope = {"task_scopes": [{"board": board, "profile": "default", "state": "ok",
                               "observed_at": time.time() - age}]}
    replica.execute("INSERT INTO replica VALUES(?,?,?,?,?)", (
        gateway, "tasks", f"default/{board}/t_owner", json.dumps(task), 1))
    replica.execute("INSERT INTO replica VALUES(?,?,?,?,?)", (gateway, "gateway", gateway, json.dumps(scope), 1))
    replica.commit()


def link(conn, tid):
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "scheduled", {"awaiting": {
            "kind": "card", "gateway": "away", "board": "default", "ref": "t_owner"}})


@pytest.mark.parametrize("variant", ["fresh", "stale", "wrong_board", "wrong_gateway", "closed"])
def test_remote_qualified_link_requires_fresh_exact_owner(conn, replica, variant):
    tid = fixtures.task(conn)
    link(conn, tid)
    publish(replica, age=2000 if variant == "stale" else 0,
            board="other" if variant == "wrong_board" else "default",
            gateway="elsewhere" if variant == "wrong_gateway" else "away",
            status="done" if variant == "closed" else "running")
    value = fixtures.pending("@task:default@away/t_owner")
    metadata = {"completion_receipt": value}
    before = fixtures.snapshot(conn)
    if variant == "fresh":
        assert kb.complete_task(conn, tid, summary="Containment complete", metadata=metadata)
        row = conn.execute("SELECT metadata FROM task_runs WHERE task_id=?", (tid,)).fetchone()
        assert json.loads(row[0])["completion_receipt"] == value
    else:
        with pytest.raises(ValueError, match="completion_receipt"):
            kb.complete_task(conn, tid, summary="Containment complete", metadata=metadata)
        assert fixtures.snapshot(conn) == before


@pytest.mark.parametrize("change", ["close", "unlink"])
def test_local_owner_rechecked_inside_transaction(conn, monkeypatch, change):
    tid = fixtures.task(conn)
    owner = fixtures.task(conn, parents=[tid])
    real = acceptance.prepare_acceptance

    def raced(*args, **kwargs):
        answer = real(*args, **kwargs)
        with kb.write_txn(conn):
            if change == "close":
                conn.execute("UPDATE tasks SET status='done' WHERE id=?", (owner,))
            else:
                conn.execute("DELETE FROM task_links WHERE child_id=?", (owner,))
        return answer

    monkeypatch.setattr(acceptance, "prepare_acceptance", raced)
    with pytest.raises(ValueError, match="completion_receipt"):
        kb.complete_task(conn, tid, summary="Containment complete", metadata={
            "completion_receipt": fixtures.pending(f"@task:default@here/{owner}")})
    assert kb.get_task(conn, tid).status != "done"
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='completed'", (tid,)).fetchone()


def sync_schema(conn, monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    monkeypatch.setattr(sync, "sync_enabled", lambda: True)
    monkeypatch.setattr(sync, "board_selected", lambda *_: True)
    sync.install_or_remove(conn, sync._db_key(conn))
    sync.set_identity(conn, "fixture-board", "here")
    sync.set_mode(conn, "off")


def test_foreign_pickup_cannot_be_named_as_local_owner(conn, monkeypatch):
    tid = fixtures.task(conn)
    owner = fixtures.task(conn, parents=[tid])
    sync_schema(conn, monkeypatch)
    conn.execute("UPDATE tasks SET created_on='away' WHERE id=?", (owner,))
    conn.commit()
    before = fixtures.snapshot(conn)
    with pytest.raises(ValueError, match="completion_receipt"):
        kb.complete_task(conn, tid, summary="Containment complete", metadata={
            "completion_receipt": fixtures.pending(f"@task:default@here/{owner}")})
    assert fixtures.snapshot(conn) == before


def test_forwarded_same_work_cannot_own_pending_followup(conn, replica):
    tid = fixtures.task(conn)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "placement_deferred", {"target": "away"})
        kb._append_event(conn, tid, "placement_forwarded", {"remote_task_id": "t_owner", "request_id": "r1"})
    publish(replica)
    before = fixtures.snapshot(conn)
    with pytest.raises(ValueError, match="completion_receipt"):
        kb.complete_task(conn, tid, summary="Containment complete", force=True, metadata={
            "completion_receipt": fixtures.pending("@task:default@away/t_owner")})
    assert fixtures.snapshot(conn) == before


def test_requester_session_is_not_pending_work_acceptance(conn, monkeypatch):
    tid = fixtures.task(conn)
    sync_schema(conn, monkeypatch)
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_chat_links "
                     "(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
                     "VALUES ('link',?,'here','default','chat','created',0,1)", (tid,))
    before = fixtures.snapshot(conn)
    with pytest.raises(ValueError, match="requester/shepherd"):
        kb.complete_task(conn, tid, summary="Containment complete", metadata={
            "completion_receipt": fixtures.pending("@session:default@here/chat")})
    assert fixtures.snapshot(conn) == before


def test_remote_link_rechecked_without_second_network_read(conn, replica, monkeypatch):
    tid = fixtures.task(conn)
    link(conn, tid)
    publish(replica)
    real = acceptance.prepare_acceptance

    def raced(*args, **kwargs):
        answer = real(*args, **kwargs)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "scheduled", {"awaiting": {"kind": "clock"}})
        return answer

    monkeypatch.setattr(acceptance, "prepare_acceptance", raced)
    with pytest.raises(ValueError, match="ownership link changed"):
        kb.complete_task(conn, tid, summary="Containment complete", metadata={
            "completion_receipt": fixtures.pending("@task:default@away/t_owner")})
    assert kb.get_task(conn, tid).status != "done"
