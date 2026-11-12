"""The notice poller gates each board on what a chat's poll could claim, not on every commit (t_01a1cee98b798343).

The main board commits ~40 times a minute with heartbeats, claims and run bookkeeping. A commit-revision gate
(``PRAGMA data_version``) opened on nearly every 5 s tick, so every live chat re-ran a full writable claim poll
of every board, and every chat's remote collect re-read the whole observation ledger, for nothing to deliver.
"""
import json
import sqlite3
import time
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tui_gateway import notification_idle
from tui_gateway import server

CHAT = "tui-session-key-1"


def _task(chat=CHAT):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="notify tui", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=chat)
        return tid
    finally:
        conn.close()


def _heartbeat(tid):
    """What a live worker writes: the task's heartbeat clock and a heartbeat event row."""
    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?", (int(time.time()), tid))
            kb._append_event(conn, tid, "heartbeat", None)
    finally:
        conn.close()


def _complete(tid):
    conn = kbc.connect()
    try:
        kb.complete_task(conn, tid, summary="all done")
    finally:
        conn.close()


def _settled(*sessions):
    for session in sessions:  # first poll records the signature; its own empty claim may move it once
        assert server._collect_kanban_notifications(session) == []
        assert server._collect_kanban_notifications(session) == []


@pytest.fixture
def full_polls(monkeypatch):
    calls = []
    real = server._kb_poll_board_now

    def counted(*args, **kwargs):
        calls.append(args[4])  # session_key
        return real(*args, **kwargs)

    monkeypatch.setattr(server, "_kb_poll_board_now", counted)
    return calls


def test_heartbeat_commit_does_not_repoll_the_board(full_polls):
    tid = _task()
    session = {"session_key": CHAT}
    _settled(session)
    full_polls.clear()
    _heartbeat(tid)
    _heartbeat(_task(chat="another-chat"))  # another chat's card: subscription insert + heartbeat
    assert server._collect_kanban_notifications(session) == []
    assert full_polls == []
    _complete(tid)  # a deliverable event still re-polls and delivers exactly once
    texts = server._collect_kanban_notifications(session)
    assert len(texts) == 1 and tid in texts[0]
    assert server._collect_kanban_notifications(session) == []


def test_board_content_is_read_once_per_commit_for_every_chat(monkeypatch):
    tid = _task()
    sessions = [{"session_key": CHAT}, {"session_key": "chat-two"}, {"session_key": "chat-three"}]
    _settled(*sessions)
    reads = []
    real = notification_idle._notify_content
    monkeypatch.setattr(notification_idle, "_notify_content", lambda *a: reads.append(1) or real(*a))
    _heartbeat(tid)
    for session in sessions:
        assert server._collect_kanban_notifications(session) == []
    assert len(reads) == 1


def test_own_cursor_rewind_repolls_and_redelivers(full_polls):
    tid = _task()
    session = {"session_key": CHAT}
    _settled(session)
    _complete(tid)
    first = server._collect_kanban_notifications(session)
    assert len(first) == 1
    [sub] = [s for s in kbn.list_notify_subs(kbc.connect(), task_id=tid) if s["chat_id"] == CHAT]
    assert server._collect_kanban_notifications(session) == []
    full_polls.clear()
    conn = kbc.connect()
    try:  # a refused turn hands the range back: no new event, only this chat's cursor moves
        assert kbn.rewind_notify_cursor(conn, task_id=tid, platform="tui", chat_id=CHAT,
                                        claimed_cursor=sub["last_event_id"], old_cursor=0)
    finally:
        conn.close()
    again = server._collect_kanban_notifications(session)
    assert full_polls == [CHAT]
    assert len(again) == 1 and tid in again[0]


def test_foreign_in_flight_claim_keeps_polling(full_polls):
    """Liveness retries for another holder's claim are unchanged: an in-flight row is never memoized quiet."""
    tid = _task()
    session = {"session_key": CHAT}
    _settled(session)
    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE kanban_notify_subs SET inflight_from = 0, inflight_pid = 1 WHERE task_id = ?",
                         (tid,))
    finally:
        conn.close()
    full_polls.clear()
    server._collect_kanban_notifications(session)
    server._collect_kanban_notifications(session)
    assert full_polls == [CHAT, CHAT]


def test_unreadable_board_is_never_unchanged(tmp_path):
    assert server._kb_board_file_sig(tmp_path / "missing.db", CHAT) is None


# ---- remote collect -----------------------------------------------------------------------------------------

def test_remote_heartbeat_does_not_reopen_the_observation_ledger(tmp_path, monkeypatch):
    from tests.tui_gateway.test_kanban_remote_notices import _card, _hub
    from tui_gateway import kanban_remote_notices as krn
    from tui_gateway import kanban_remote_observations as obs
    now = 1_800_000_000.0
    done = _card("t_done_remote", CHAT, status="done", at=now - 60)
    replica = _hub(tmp_path, [done])
    with sqlite3.connect(replica) as conn:
        conn.executemany("INSERT INTO meta VALUES (?,?)", [("cursor", "100"), ("head", "100")])
        conn.execute("INSERT INTO replica VALUES ('hub','tasks','default/default/t_live',?,2)",
                     (json.dumps({"id": "t_live", "status": "running"}),))
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    first = obs.observe(replica, ledger, now)
    assert first

    opened = []
    real = krn._ledger
    monkeypatch.setattr(krn, "_ledger", lambda *a, **kw: opened.append(1) or real(*a, **kw))
    with sqlite3.connect(replica) as conn:  # the owner's heartbeat on a running card moves the feed
        conn.execute("UPDATE replica SET seq = 3, data = ? WHERE key = 'default/default/t_live'",
                     (json.dumps({"id": "t_live", "status": "running", "hb": 1}),))
    assert obs.observe(replica, ledger, now + 5) == first
    assert opened == []

    later = _card("t_later_remote", CHAT, status="done", at=now + 5)
    with sqlite3.connect(replica) as conn:  # a new terminal state is recorded once, with the ledger's clock
        conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,4)", (later["key"], json.dumps(later["task"])))
    fresh = obs.observe(replica, ledger, now + 10)
    assert opened == [1]
    assert {k: v for k, v in fresh.items() if k[1] != later["key"]} == first
    with sqlite3.connect(ledger) as conn:
        rows = conn.execute("SELECT ref, first_seen FROM observations ORDER BY ref").fetchall()
    assert [ref for ref, _ in rows] == ["hub/default/default/t_done_remote", "hub/default/default/t_later_remote"]
    assert dict(rows)["hub/default/default/t_later_remote"] == now + 10

    krn._reset_for_tests()  # a restarted backend reads the same first_seen back from the ledger
    assert obs.observe(replica, ledger, now + 20) == fresh


def test_session_lineage_is_reused_between_polls(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("root", "desktop")
        db.create_session("fork", "desktop", parent_session_id="root")
    assert server._session_lineage("fork") == ("root",)
    with patch.object(sqlite3, "connect", side_effect=AssertionError("re-read state.db")):
        assert server._session_lineage("fork") == ("root",)
        server._lineage_memo.clear()
        assert server._session_lineage("fork") == ()  # unreadable: no lineage, and nothing remembered
    assert server._session_lineage("fork") == ("root",)
