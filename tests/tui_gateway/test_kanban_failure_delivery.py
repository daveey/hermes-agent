"""Canonical failures survive split polling and consumer restart in real stores."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn
from hermes_state import SessionDB
from tui_gateway import server as notices


@pytest.mark.parametrize("split", [True, False])
def test_failure_notice_persisted_once_per_run(tmp_path, split):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    db.create_session("failure-chat", source="desktop")
    session = {"session_key": "failure-chat", "profile_home": str(tmp_path), "agent": SimpleNamespace(session_db=db)}
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="failure fixture", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="failure-chat")

        def emit(kind, **payload):
            with kb.write_txn(conn):
                kb._append_event(conn, tid, kind, payload)

        def poll():
            claims = []
            texts = notices._collect_kanban_notifications(session, claims)
            session["_kanban_claims"] = claims
            if not texts:
                return "empty"
            verdict, _, _ = notices._kb_persist_batch(session, texts, True)
            assert verdict in {"written", "delivered"}, verdict
            notices._kb_ack_durable(session, {id(t) for t in texts})
            return verdict

        emit("reclaimed", failure_notice="canonical", run_uid="run-one")
        if split:
            assert poll() == "empty"
        emit("crashed", failure_notice="canonical", run_uid="run-one")
        emit("gave_up", failure_notice="canonical", run_uid="run-one", failures=3, effective_limit=2)
        emit("failure_finalized", run_uid="run-one", final_status="triage", failures=3, effective_limit=2)
        if not split:
            emit("failure_finalized", run_uid="run-one", final_status="triage", failures=3, effective_limit=2)
        assert poll() == "written"
        rows = db.get_messages("failure-chat")
        assert len(rows) == 1
        assert rows[0]["content"].count("triage") == 1
        assert "3 of 2" not in rows[0]["content"]
        assert "returned to ready" not in rows[0]["content"]
        assert "triage" in rows[0]["content"]

        # A new event ID for the same run after a restarted consumer must not
        # create a second transcript row. No in-memory key cache survives here.
        db.close()
        db = SessionDB(db_path)
        session = {"session_key": "failure-chat", "profile_home": str(tmp_path), "agent": SimpleNamespace(session_db=db)}
        emit("failure_finalized", run_uid="run-one", final_status="triage", failures=3, effective_limit=2)
        assert poll() == "delivered"
        assert len(db.get_messages("failure-chat")) == 1

        emit("failure_finalized", run_uid="run-two", final_status="ready", failures=1, effective_limit=2)
        assert poll() == "written"
        assert len(db.get_messages("failure-chat")) == 2
    finally:
        conn.close()
        db.close()
