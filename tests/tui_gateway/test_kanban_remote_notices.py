"""A remote owner publishes completion evidence; the destination persists one passive notice."""

import contextlib
import json
import sqlite3

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli.kanban_origin_ledger import observe_owner_task
from hermes_state import SessionDB
from tui_gateway.kanban_remote_notices import sweep_remote_notices

CHAT = "20261026_235722_bef6c6"
OTHER = "20261022_204322_4d8ce1"


def _replicate(tmp_path, summary, *, legacy=False):
    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="remote worker", assignee="default")
        for sid in (CHAT, OTHER):
            kbn.add_notify_sub(conn, task_id=tid, platform="hub", chat_id=f"macbook/default/{sid}")
        kb.complete_task(conn, tid, summary=summary)
        if legacy:
            conn.execute("UPDATE task_events SET payload=json_remove(payload,'$.notice_version') WHERE task_id=?", (tid,))
            conn.commit()
        task = {"id": tid, "status": "done", "title": "remote worker", "board": "default",
                "gateway": "hub", "profile": "default", "assignee": "default", "origin_bindings": [],
                "sessions": [{"gw": "macbook", "profile": "default", "session": sid, "rel": "shepherd"}
                             for sid in (CHAT, OTHER)]}
        conn.execute("BEGIN")
        published = observe_owner_task(conn, task, now=0)
        conn.rollback()
    replica = tmp_path / "hub.db"
    with sqlite3.connect(replica) as conn:
        conn.execute("CREATE TABLE replica(gw TEXT,coll TEXT,key TEXT,data TEXT)")
        conn.execute("INSERT INTO replica VALUES (?,?,?,?)", ("hub", "tasks", f"default/default/{tid}", json.dumps(published)))
    return tid, replica


def test_remote_completion_reaches_only_addressed_chat_and_is_idempotent(tmp_path):
    tid, replica = _replicate(tmp_path, f"FINAL SEND for @session:default@macbook/{CHAT}: remote proof")
    with SessionDB(tmp_path / "state.db") as db:
        for sid in (CHAT, OTHER):
            db.create_session(sid, "desktop")

        @contextlib.contextmanager
        def opener(profile, sid):
            assert profile == "default"
            yield db, db.get_compression_tip(sid) or sid

        for _ in range(2):
            out = sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener)
            assert len(out) == 2
        messages = db.get_messages(CHAT)
        assert len(messages) == 1
        assert messages[0]["display_kind"] == "kanban_notice"
        assert "remote proof" in messages[0]["content"] and tid in messages[0]["content"]
        assert "remote proof" not in db.get_messages(OTHER)[0]["content"]
        db.end_session(CHAT, "compression")
        db.create_session("continuation", "desktop", parent_session_id=CHAT)
        sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener)
        assert not db.get_messages("continuation")
        assert len(db.get_messages(CHAT)) == 1
        assert not sweep_remote_notices(replica_path=replica, gateway="stranger", session_db=opener)


def test_remote_generic_notice_and_busy_retry(tmp_path):
    _tid, replica = _replicate(tmp_path, "generic completion proof")
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")
        busy = True

        @contextlib.contextmanager
        def opener(profile, sid):
            if busy:
                raise RuntimeError("busy")
            yield (db, sid) if sid == CHAT else (None, None)

        assert not sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener)
        busy = False
        assert len(sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener)) == 1
        assert "generic completion proof" in db.get_messages(CHAT)[0]["content"]


def test_legacy_done_cards_are_not_replayed_on_rollout(tmp_path):
    _tid, replica = _replicate(tmp_path, "historical completion", legacy=True)
    assert not sweep_remote_notices(replica_path=replica, gateway="macbook")
    with sqlite3.connect(replica) as conn:
        task = json.loads(conn.execute("SELECT data FROM replica").fetchone()[0])
        assert "completion_notice" not in task


def test_absent_replica_is_read_only_and_not_created(tmp_path):
    path = tmp_path / "absent.db"
    assert sweep_remote_notices(replica_path=path, gateway="macbook") == []
    assert not path.exists()
