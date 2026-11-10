"""Archiving a pending human request must not leave its chat asking forever."""
from contextlib import closing, contextmanager
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tui_gateway import server


@pytest.mark.parametrize("status,human,expected", [
    ("blocked", '{"action":"Choose"}', True),
    ("blocked", None, False),
    ("ready", '{"action":"stale"}', False),
    ("done", None, False),
])
def test_archive_request_notice_is_atomic_and_idempotent(status, human, expected):
    with closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="Decision", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="withdrawal-chat")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=?, human_request=? WHERE id=?", (status, human, tid))
        assert kb.archive_task(conn, tid)
        assert not kb.archive_task(conn, tid)
        claims = []
        texts = server._collect_kanban_notifications({"session_key": "withdrawal-chat"}, claims)
        assert len(texts) == int(expected)
        if expected:
            assert "request withdrawn" in texts[0]
            assert "not approval" in texts[0]
            assert "not proof of completion" in texts[0]
            assert len(kbn.list_notify_subs(conn, task_id=tid)) == 1
            assert len(claims) == 1
            claim = claims[0]
            assert kbn.ack_notify_claim(conn, task_id=tid, platform="tui", chat_id="withdrawal-chat",
                                        thread_id="", upto=claim["new"], from_cursor=claim["old"])
        assert kbn.list_notify_subs(conn, task_id=tid) == []
        assert server._collect_kanban_notifications({"session_key": "withdrawal-chat"}) == []


@pytest.mark.parametrize("destination", ["open", "archived", "moved"])
def test_unloaded_withdrawal_retries_failed_persist(tmp_path, monkeypatch, destination):
    from hermes_state import SessionDB
    from tui_gateway import kanban_unloaded as ku

    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_WAKE", "0")
    with closing(SessionDB(tmp_path / "state.db")) as db, closing(kbc.connect()) as conn:
        sid = "withdrawal-unloaded"
        db.create_session(sid, "desktop")
        db.append_message(sid, "user", "Check this")
        db.append_message(sid, "assistant", "Please choose")
        tid = kb.create_task(conn, title="Decision", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=sid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked', human_request=? WHERE id=?",
                         ('{"action":"Choose"}', tid))
        kb.archive_task(conn, tid)

        @contextmanager
        def opener(chat_id):
            yield db, sid

        if destination != "open":
            db._write_rowcount("UPDATE sessions SET archived=?, end_reason=? WHERE id=?",
                              (int(destination == "archived"),
                               "moved_to_gateway:other" if destination == "moved" else None, sid))
            assert ku.sweep_once(set(), now=time.time() + 3600, session_db=opener) == []
            assert not [m for m in db.get_messages(sid) if m.get("display_kind") == "kanban_notice"]
            return

        with monkeypatch.context() as m:
            def fail(*args, **kwargs):
                raise RuntimeError("write unavailable")
            m.setattr(db, "append_kanban_notice", fail)
            assert ku.sweep_once(set(), now=time.time() + 3600, session_db=opener) == []
        assert len(kbn.list_notify_subs(conn, task_id=tid)) == 1
        assert len(ku.sweep_once(set(), now=time.time() + 3600, session_db=opener)) == 1
        notices = [m for m in db.get_messages(sid) if m.get("display_kind") == "kanban_notice"]
        assert len(notices) == 1
        assert "not proof of completion" in notices[0]["content"]
        from hermes_state_outcome import get_outcome
        outcome = get_outcome(sid, db=db)
        assert outcome is not None
        assert outcome["kind"] == "unresolved"
        assert outcome["reason"] == "notice_tail"
        assert kbn.list_notify_subs(conn, task_id=tid) == []
        assert ku.sweep_once(set(), now=time.time() + 3600, session_db=opener) == []


def test_older_claim_ack_preserves_unclaimed_withdrawal():
    with closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="Decision", assignee="worker")
        ident = dict(task_id=tid, platform="tui", chat_id="old-claim", thread_id="")
        kbn.add_notify_sub(conn, **ident)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked', human_request=? WHERE id=?",
                         ('{"action":"Choose"}', tid))
            kb._append_event(conn, tid, "blocked", {})
        claims = []
        assert server._collect_kanban_notifications({"session_key": "old-claim"}, claims)
        kb.archive_task(conn, tid)
        assert kbn.ack_notify_claim(conn, upto=claims[0]["new"], from_cursor=claims[0]["old"], **ident)
        assert len(kbn.list_notify_subs(conn, task_id=tid)) == 1
        texts = server._collect_kanban_notifications({"session_key": "old-claim"})
        assert len(texts) == 1 and "request withdrawn" in texts[0]


def test_dispatched_withdrawal_keeps_claim_until_worker_persists(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from tui_gateway import kanban_unloaded as ku

    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_WAKE", "1")
    ku._reset_wake_budget_for_tests()
    pending = []
    def spawn(job):
        pending.append(job)
        return "dispatched"
    with closing(SessionDB(tmp_path / "state.db")) as db, closing(kbc.connect()) as conn:
        sid = "withdrawal-dispatched"
        db.create_session(sid, "desktop")
        tid = kb.create_task(conn, title="Decision", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=sid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked', human_request=? WHERE id=?",
                         ('{"action":"Choose"}', tid))
        kb.archive_task(conn, tid)
        @contextmanager
        def opener(chat_id):
            yield db, sid
        def fail(*args, **kwargs):
            raise RuntimeError("write unavailable")
        try:
            assert len(ku.sweep_once(set(), now=time.time() + 3600, session_db=opener,
                                    wake=lambda *args: "refused", holders=list, spawn=spawn)) == 1
            assert len(kbn.list_notify_subs(conn, task_id=tid)) == 1
            with monkeypatch.context() as m:
                m.setattr(db, "append_kanban_notice", fail)
                assert pending.pop()() == "error"
            assert len(kbn.list_notify_subs(conn, task_id=tid)) == 1
            assert len(ku.sweep_once(set(), now=time.time() + 3600, session_db=opener,
                                    wake=lambda *args: "refused", holders=list, spawn=spawn)) == 1
            assert pending.pop()() == "refused"
            assert kbn.list_notify_subs(conn, task_id=tid) == []
            assert len([m for m in db.get_messages(sid) if m.get("display_kind") == "kanban_notice"]) == 1
        finally:
            for job in pending:
                job()


def test_request_withdrawal_is_deliverable_on_both_surfaces():
    from gateway import kanban_watchers_notifier as notifier
    ev = SimpleNamespace(kind="request_withdrawn", payload={})
    task = SimpleNamespace(title="Decision", assignee="worker")
    text = server._format_kanban_event_text({"task_id": "t_example"}, task, ev, "default")
    assert "not approval" in text
    assert "request_withdrawn" in notifier.TERMINAL_KINDS
    assert "request_withdrawn" in notifier._WAKE_KINDS
    rendered, _, _ = notifier._EVENT_FORMATTERS["request_withdrawn"](ev, SimpleNamespace(head="Decision"))
    assert "not proof of completion" in rendered
