"""A chat whose agent build failed is still the sweeper's to deliver to. [daveey]

Hub chat 20261028_221818_fdf44b: every resume died at agent build ("Exact pinned route cannot fail over"). The failed
record stayed in ``server._sessions`` with no agent and no notification poller, ``live_session_ids`` counted its
key as loaded, and the sweeper skipped the chat, so a completed card's FINAL SEND waited with nobody to claim it.
"""

import contextlib
import threading
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_state import SessionDB
from tui_gateway import kanban_unloaded as ku
from tui_gateway import server

CHAT = "20261028_221818_fdf44b"
LATER = time.time() + 3600
FINAL = ("Ask Eve update: PR passed review. " + "Full detail sentence. " * 30).strip()


@pytest.fixture
def db(tmp_path):
    d = SessionDB(tmp_path / "state.db")
    d.create_session(CHAT, "desktop")
    d.append_message(CHAT, "user", "go")
    d.append_message(CHAT, "assistant", "ok")
    return d


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_UNLOADED_WAKE", raising=False)
    ku._reset_wake_budget_for_tests()
    yield
    ku._reset_wake_budget_for_tests()
    with server._sessions_lock:
        server._sessions.pop("deadbuild", None)


def _record(*, error, agent=None, ready=True):
    event = threading.Event()
    if ready:
        event.set()
    return {"agent": agent, "agent_error": error, "agent_ready": event, "session_key": CHAT}


def _opener(db):
    @contextlib.contextmanager
    def opener(chat_id):
        yield (db, db.get_compression_tip(chat_id) or chat_id) if db.get_session(chat_id) else (None, None)
    return opener


def _final_send_card():
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="relay", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=CHAT)
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary=f"done\nFINAL SEND for @session:default/{CHAT}: {FINAL}")
        return tid
    finally:
        conn.close()


def test_failed_build_record_does_not_count_as_loaded():
    with server._sessions_lock:
        server._sessions["deadbuild"] = _record(error="ModelRoutingError: Exact pinned route cannot fail over")
    assert CHAT not in ku.live_session_ids()


@pytest.mark.parametrize("record", [
    _record(error=None, agent=object()),                                   # built agent
    _record(error=None, ready=False),                                      # build still running
    _record(error="late failure", agent=object()),                         # has an agent: its poller owns it
], ids=["built", "building", "agent-present"])
def test_other_records_still_count_as_loaded(record):
    with server._sessions_lock:
        server._sessions["deadbuild"] = record
    assert CHAT in ku.live_session_ids()


def test_final_send_is_delivered_in_full_without_a_model(db):
    with server._sessions_lock:
        server._sessions["deadbuild"] = _record(error="ModelRoutingError: Exact pinned route cannot fail over")
    _final_send_card()
    done = ku.sweep_once(ku.live_session_ids(), now=LATER, session_db=_opener(db), spawn=lambda job: job())
    assert len(done) == 1
    rows = [m for m in db.get_messages(CHAT) if m.get("display_kind") == "kanban_notice"]
    assert len(rows) == 1
    assert FINAL in (rows[0]["content"] or "")  # complete, not truncated
