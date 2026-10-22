"""Kanban results reach a chat that no backend holds (t_15e965e8). [daveey]

Session 68bf46 started cards, its backend died and the orphan reap closed the row; the completion notices sat
unclaimed for five days because ``_kb_poll_board`` only runs for sessions loaded in memory.
"""

import contextlib
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_state import SessionDB
from tui_gateway import kanban_unloaded as ku
from tui_gateway.server import _collect_kanban_notifications

CHAT = "20261014_205511_68bf46"


@pytest.fixture
def db(tmp_path):
    d = SessionDB(tmp_path / "state.db")
    d.create_session(CHAT, "desktop")
    d.append_message(CHAT, "user", "start the cards")
    d.append_message(CHAT, "assistant", "started")
    return d


def _session_db(db):
    @contextlib.contextmanager
    def opener(chat_id):
        if db.get_session(chat_id) is None:
            yield None, None
        else:
            yield db, db.get_compression_tip(chat_id) or chat_id
    return opener


def _card(chat_id=CHAT, summary="shipped"):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="unloaded card", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=chat_id)
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _cursor(tid):
    conn = kbc.connect()
    try:
        return kbn.list_notify_subs(conn, task_id=tid)[0]["last_event_id"]
    finally:
        conn.close()


def _notices(db):
    return [m for m in db.get_messages(CHAT) if m.get("display_kind") == "kanban_notice"]


LATER = time.time() + 3600  # every event is older than the grace window


def test_notice_persisted_within_one_sweep_exactly_once_and_marks_unread(db):
    tid = _card()
    assert db.session_unread(db.get_session(CHAT)) is False  # NULL watermark = read

    out = ku.sweep_once(set(), now=LATER, session_db=_session_db(db))

    assert [d["task_id"] for d in out] == [tid]
    notices = _notices(db)
    assert len(notices) == 1 and tid in notices[0]["content"] and "shipped" in notices[0]["content"]
    assert notices[0]["role"] == "user"
    # visible on reload: the stored transcript carries it, and the list row reads unread
    row = next(s for s in db.list_sessions_rich() if s["id"] == CHAT)
    assert row["unread"] is True
    # second sweep: cursor already claimed, nothing duplicated
    assert ku.sweep_once(set(), now=LATER, session_db=_session_db(db)) == []
    assert len(_notices(db)) == 1


def test_live_poller_and_sweeper_never_double_deliver(db):
    _card()
    # live poller claims first (loaded session)
    texts = _collect_kanban_notifications({"session_key": CHAT})
    assert len(texts) == 1
    assert ku.sweep_once(set(), now=LATER, session_db=_session_db(db)) == []
    assert _notices(db) == []

    # and the other way round: sweeper first, then the live poller finds nothing
    _card(summary="second")
    assert len(ku.sweep_once(set(), now=LATER, session_db=_session_db(db))) == 1
    assert _collect_kanban_notifications({"session_key": CHAT}) == []
    assert len(_notices(db)) == 1


def test_loaded_session_is_skipped(db):
    tid = _card()
    before = _cursor(tid)
    assert ku.sweep_once({CHAT}, now=LATER, session_db=_session_db(db)) == []
    assert _notices(db) == [] and _cursor(tid) == before


def test_fresh_event_inside_grace_is_left_to_a_live_poller(db):
    tid = _card()
    before = _cursor(tid)
    assert ku.sweep_once(set(), grace_s=3600, now=time.time(), session_db=_session_db(db)) == []
    assert _cursor(tid) == before


def test_failed_append_rewinds_so_the_notice_is_retried(db, monkeypatch):
    tid = _card()
    before = _cursor(tid)
    with monkeypatch.context() as m:
        m.setattr(db, "append_kanban_notice", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("busy")))
        assert ku.sweep_once(set(), now=LATER, session_db=_session_db(db)) == []
        assert _cursor(tid) == before
    assert len(ku.sweep_once(set(), now=LATER, session_db=_session_db(db))) == 1


def test_unknown_chat_keeps_its_cursor(db):
    tid = _card(chat_id="no-such-chat")
    before = _cursor(tid)
    assert ku.sweep_once(set(), now=LATER, session_db=_session_db(db)) == []
    assert _cursor(tid) == before


def test_append_is_deduped_by_notice_id(db):
    meta = {"notice_id": "default:t_x:0-1"}
    first = db.append_kanban_notice(CHAT, "one", meta)
    assert db.append_kanban_notice(CHAT, "one", meta) == first
    assert len(_notices(db)) == 1


def test_open_subscription_sessions_reports_reaped_chats():
    _card()
    assert ku.open_subscription_sessions([CHAT, "other"]) == {CHAT: 1}
    assert ku.open_subscription_sessions([]) == {}


def test_startup_orphan_reap_warns_about_reaped_chats_holding_subscriptions(monkeypatch, caplog):
    import logging

    from tui_gateway import server

    class _Db:
        def sweep_orphaned_sessions(self, **_kw):
            return [CHAT, "plain-session"]

    _card()
    monkeypatch.setattr(server, "_get_db", lambda: _Db())
    monkeypatch.setattr(server, "_SESSION_TTL_S", 3600)
    with caplog.at_level(logging.WARNING):
        assert server._sweep_orphaned_session_rows() == [CHAT, "plain-session"]
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(CHAT in m and "kanban subscriptions" in m and "plain-session" not in m for m in warned)
