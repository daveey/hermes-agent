"""The unloaded-chat sweeper through the REAL store opener, and a chat mid-turn. [daveey]

2026-10-27: a card finished while its chat ran a 2h turn. ``append_kanban_notice`` correctly refused
("Session has an active turn lease"), but ``find_session_db`` caught that error at its ``yield``, yielded a
second time and Python raised "generator didn't stop after throw()". The sweeper logged that as a
failure with a traceback every 30 s for 2h20m (277 WARNINGs, kanban-e5babca565). The older tests swapped in a
fake opener, so the real one never saw an error from the caller's block.
"""

import contextlib
import logging
import os
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_state import SessionDB
from hermes_state_errors import SessionTurnLeaseLostError
from tui_gateway import kanban_unloaded as ku

CHAT = "20261027_085427_ac4078"
HOLDER = f"pid={os.getpid()}:test-turn"  # a live holder: never reclaimed as dead
LOGGER = "tui_gateway.kanban_unloaded"


@pytest.fixture
def db(tmp_path, monkeypatch):
    from tui_gateway import server

    d = SessionDB(tmp_path / "state.db")
    d.create_session(CHAT, "desktop")
    d.append_message(CHAT, "user", "run the long job")

    @contextlib.contextmanager
    def profile_db(params=None, *, writer=False):
        yield d if not params else None  # launch store holds the chat; the named profile has none

    monkeypatch.setattr(server, "_profile_db", profile_db)
    monkeypatch.setattr('tui_gateway.kanban_chat_hosting.profile_roster', lambda: ([None, 'other'], True))
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_WAKE", "0")
    ku._reset_overdue_for_tests()
    yield d
    ku._reset_overdue_for_tests()


def _card():
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="long card", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=CHAT)
        kb.complete_task(conn, tid, summary="shipped")
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


def _records(caplog, level):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == level]


def test_error_inside_the_real_opener_comes_out_as_itself(db):
    """The caller's own error must not become "generator didn't stop after throw()"."""
    with pytest.raises(SessionTurnLeaseLostError, match="active turn lease"):
        with ku.find_session_db(CHAT) as (store, tip):
            assert store is db and tip == CHAT
            raise SessionTurnLeaseLostError("Session has an active turn lease; refusing transcript mutation")


def test_store_open_errors_are_still_skipped(db, monkeypatch):
    from tui_gateway import server

    @contextlib.contextmanager
    def broken_then_ok(params=None, *, writer=False):
        if not params:
            raise OSError("launch store unavailable")
        yield db

    monkeypatch.setattr(server, "_profile_db", broken_then_ok)
    with ku.find_session_db(CHAT) as (store, tip):
        assert store is db and tip == CHAT


def test_busy_chat_defers_quietly_and_lands_after_the_turn(db, caplog):
    tid = _card()
    before = _cursor(tid)
    assert db.try_acquire_session_turn_lease(CHAT, HOLDER, ttl_seconds=300.0)
    soon = time.time() + 60  # past the grace window, well inside the overdue window

    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert ku.sweep_once(set(), now=soon) == []
        assert ku.sweep_once(set(), now=soon) == []
    assert _cursor(tid) == before and _notices(db) == []  # rewound: retried, not lost
    assert _records(caplog, logging.WARNING) == []
    assert not any(r.exc_info for r in caplog.records if r.name == LOGGER)
    assert [r.getMessage() for r in _records(caplog, logging.INFO) if "deferred: chat busy" in r.getMessage()]

    db.release_session_turn_lease(CHAT, HOLDER)
    out = ku.sweep_once(set(), now=soon)
    assert [d["task_id"] for d in out] == [tid]
    assert len(_notices(db)) == 1


def test_a_notice_stalled_past_the_overdue_window_warns_once(db, caplog):
    tid = _card()
    assert db.try_acquire_session_turn_lease(CHAT, HOLDER, ttl_seconds=300.0)
    later = time.time() + 3600

    with caplog.at_level(logging.INFO, logger=LOGGER):
        for _ in range(3):
            assert ku.sweep_once(set(), now=later) == []
    warned = [r.getMessage() for r in _records(caplog, logging.WARNING)]
    assert len(warned) == 1, warned
    assert "overdue" in warned[0] and tid in warned[0] and CHAT in warned[0] and "chat busy" in warned[0]

    db.release_session_turn_lease(CHAT, HOLDER)
    assert len(ku.sweep_once(set(), now=later)) == 1


def test_wake_path_refused_then_busy_row_defers_quietly(db, monkeypatch, caplog):
    """Same deferral when the notice goes through the wake worker (wake refused -> passive row -> chat busy)."""
    monkeypatch.delenv("HERMES_KANBAN_UNLOADED_WAKE", raising=False)
    ku._reset_wake_budget_for_tests()
    tid = _card()
    before = _cursor(tid)
    assert db.try_acquire_session_turn_lease(CHAT, HOLDER, ttl_seconds=300.0)
    results = []

    def inline(job):
        results.append(job())
        return "dispatched"

    with caplog.at_level(logging.INFO, logger=LOGGER):
        ku.sweep_once(set(), now=time.time() + 60, wake=lambda *a: "refused:attached", holders=list,
                      spawn=inline)
    assert results == ["deferred"]
    assert _cursor(tid) == before and _notices(db) == []
    assert _records(caplog, logging.WARNING) == []
    assert any("deferred: chat busy" in r.getMessage() for r in _records(caplog, logging.INFO))
