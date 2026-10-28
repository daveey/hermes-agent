"""A kanban notice claimed for a busy chat must survive the claiming backend dying. [daveey]

Live incident (hub t_01a1835174955959, MacBook 2026-10-28 06:25-06:37 PDT): cards t_01a18241ca7343ac and
t_01a1827a23ed6284 completed while chats 5ab65c and f07c32 were mid-turn. Their pollers claimed the completed
events (``last_event_id`` advanced to 166944 / 166963) into the in-memory ``_kanban_pending`` buffer, waiting for
the turn to end. A core-update drain then held new turns and the serve process was replaced; the buffer died with
it. The board said "delivered", the chats never got a row (state.db had none), and nothing ever retried.

Fix: the tui poller's claim is recorded IN FLIGHT with its holder process and acknowledged only once the notice is
in the transcript (a ``kanban_notice`` row written before the turn, deduped by event id). A claim whose holder
process is gone is rewound, so the chat's next poller (or the unloaded sweep) delivers it.
"""

import contextlib
import subprocess
import sys
import threading
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_state import SessionDB
from tui_gateway import kanban_unloaded as ku
from tui_gateway import server

KEY = "20261027_134353_5ab65c"


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _session(key: str = KEY, running: bool = False) -> dict:
    return {"session_key": key, "history_lock": threading.RLock(), "running": running}


def _task(chat_id: str = KEY) -> str:
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="Drill parent", assignee="default")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=chat_id)
        return tid
    finally:
        conn.close()


def _complete(tid: str, summary: str = "reply OK and complete") -> None:
    conn = kbc.connect()
    try:
        assert kb.complete_task(conn, tid, summary=summary)
    finally:
        conn.close()


def _sub(tid: str) -> dict:
    conn = kbc.connect()
    try:
        row = conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id = ?", (tid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def _orphan(tid: str, inflight_from: int) -> None:
    """The claiming backend died before delivering: its in-flight claim names a dead process."""
    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE kanban_notify_subs SET inflight_from = ?, inflight_pid = ?, inflight_start = NULL "
                         "WHERE task_id = ?", (inflight_from, _dead_pid(), tid))
    finally:
        conn.close()


@pytest.fixture
def gate(monkeypatch):
    state = {"submitted": []}

    def _submit(rid, sid, session, text, **kwargs):
        state["submitted"].append((sid, text, kwargs))
        with session["history_lock"]:
            session["running"] = False
        return True

    monkeypatch.setattr(server, "_run_prompt_submit", _submit)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    return state


@pytest.fixture
def sdb(tmp_path, monkeypatch):
    d = SessionDB(tmp_path / "state.db")
    d.create_session(KEY, "desktop")
    d.append_message(KEY, "user", "run the drill")

    @contextlib.contextmanager
    def _opener(session):
        yield d

    monkeypatch.setattr(server, "_session_db", _opener)
    return d


def _notices(d: SessionDB) -> list:
    return [m for m in d.get_messages(KEY) if m.get("display_kind") == "kanban_notice"]


def test_notice_claimed_while_busy_survives_backend_replacement(gate):
    """The incident: claimed during a long turn, the backend is replaced before the turn ends."""
    tid = _task()
    before = _sub(tid)["last_event_id"]
    _complete(tid)

    busy = _session(running=True)
    server._notif_poll_kanban_scoped("ui-old-backend", busy)
    assert gate["submitted"] == [] and len(busy["_kanban_pending"]) == 1, "buffered while the turn runs"
    assert _sub(tid)["last_event_id"] > before, "claimed (the cursor the incident showed as 'delivered')"
    assert _sub(tid)["inflight_from"] == before, "but still in flight, owned by this process"

    # The old backend dies with the notice in memory (the core-update replaced serve).
    _orphan(tid, _sub(tid)["inflight_from"])

    server._notif_poll_kanban_scoped("ui-new-backend", _session())
    assert len(gate["submitted"]) == 1 and tid in gate["submitted"][0][1], "the new backend redelivers it"


def test_notice_row_is_written_before_the_turn_and_the_claim_acked(gate, sdb):
    tid = _task()
    _complete(tid)
    session = _session()

    server._notif_poll_kanban_scoped("ui", session)

    rows = _notices(sdb)
    assert len(rows) == 1 and tid in rows[0]["content"]
    assert len(gate["submitted"]) == 1
    _sid, text, kwargs = gate["submitted"][0]
    assert text == rows[0]["content"] and kwargs.get("display_kind") == "kanban_notice"
    assert session["_submit_user_row"]["content"] == text, "the turn adopts the row instead of writing a second"
    sub = _sub(tid)
    assert sub["inflight_from"] is None and sub["inflight_pid"] is None, "acked once durable"


def test_redelivery_after_a_crash_between_write_and_ack_does_not_duplicate(gate, sdb):
    tid = _task()
    before = _sub(tid)["last_event_id"]
    _complete(tid)
    server._notif_poll_kanban_scoped("ui", _session())
    assert len(_notices(sdb)) == 1 and len(gate["submitted"]) == 1

    _orphan(tid, before)  # died after the row was written, before the ack
    server._notif_poll_kanban_scoped("ui-new", _session())

    assert len(_notices(sdb)) == 1, "deduped by event id"
    assert len(gate["submitted"]) == 1, "no second turn for a notice already in the chat"
    assert _sub(tid)["inflight_from"] is None


def test_busy_transcript_keeps_the_claim_in_flight_and_buffered(gate, sdb, monkeypatch):
    from hermes_state_errors import SessionTurnLeaseLostError

    def _busy(*a, **k):
        raise SessionTurnLeaseLostError("another backend runs a turn")

    monkeypatch.setattr(sdb, "record_kanban_notice", _busy)
    tid = _task()
    before = _sub(tid)["last_event_id"]
    _complete(tid)
    session = _session()

    server._notif_poll_kanban_scoped("ui", session)

    assert gate["submitted"] == [] and len(session["_kanban_pending"]) == 1
    assert session["running"] is False
    assert _sub(tid)["inflight_from"] == before


def test_live_holder_blocks_other_claimers_and_is_not_recovered():
    tid = _task()
    _complete(tid)
    conn = kbc.connect()
    try:
        old, _new, events = kbn.claim_unseen_events_for_sub(conn, task_id=tid, platform="tui", chat_id=KEY,
                                                           durable=True)
        assert events
        with kb.write_txn(conn):  # pretend a different, LIVE process holds it (our parent)
            import os
            conn.execute("UPDATE kanban_notify_subs SET inflight_pid = ?, inflight_start = NULL WHERE task_id = ?",
                         (os.getppid(), tid))
            conn.execute("UPDATE kanban_notify_subs SET last_event_id = ? WHERE task_id = ?", (old, tid))
        assert kbn.recover_orphaned_notify_claims(conn) == 0
        assert kbn.claim_unseen_events_for_sub(conn, task_id=tid, platform="tui", chat_id=KEY,
                                               durable=True)[2] == []
    finally:
        conn.close()


def test_ack_keeps_an_earlier_outstanding_claim_in_flight():
    tid = _task()
    conn = kbc.connect()
    try:
        kb.block_task(conn, tid, reason="first")
        o1, n1, e1 = kbn.claim_unseen_events_for_sub(conn, task_id=tid, platform="tui", chat_id=KEY, durable=True)
        kb.unblock_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="second")
        o2, n2, e2 = kbn.claim_unseen_events_for_sub(conn, task_id=tid, platform="tui", chat_id=KEY, durable=True)
        assert e1 and e2 and o2 == n1

        kbn.ack_notify_claim(conn, task_id=tid, platform="tui", chat_id=KEY, upto=n2, from_cursor=o2)
        assert _sub(tid)["inflight_from"] == o1, "the first claim is still undelivered"
        kbn.ack_notify_claim(conn, task_id=tid, platform="tui", chat_id=KEY, upto=n2, from_cursor=o1)
        assert _sub(tid)["inflight_from"] is None
    finally:
        conn.close()


def test_unloaded_sweep_recovers_an_orphaned_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_WAKE", "0")
    d = SessionDB(tmp_path / "state.db")
    d.create_session(KEY, "desktop")
    d.append_message(KEY, "user", "start")
    tid = _task()
    before = _sub(tid)["last_event_id"]
    _complete(tid)
    conn = kbc.connect()
    try:
        assert kbn.claim_unseen_events_for_sub(conn, task_id=tid, platform="tui", chat_id=KEY, durable=True)[2]
    finally:
        conn.close()
    _orphan(tid, before)

    @contextlib.contextmanager
    def opener(chat_id):
        yield (d, chat_id) if d.get_session(chat_id) else (None, None)

    out = ku.sweep_once(set(), now=time.time() + 3600, session_db=opener)

    assert [o["task_id"] for o in out] == [tid]
    assert len(_notices(d)) == 1 and tid in _notices(d)[0]["content"]
