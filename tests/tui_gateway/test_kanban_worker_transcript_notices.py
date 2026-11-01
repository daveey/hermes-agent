"""A Kanban worker transcript never gets a card notice or a wake. [daveey]

Hub card t_a989e5e0 finished 2026-10-31 17:37:58Z. Five MacBook worker transcripts of another card held hermes-hub
shepherd subs on it (copied onto it when it was linked under t_ae24b36d), so the MacBook's unloaded sweeper wrote the
done notice into each transcript and tried to wake each one; every wake was refused (``session.resume`` 4132: "is a
Kanban worker transcript"). A worker transcript is a ``local-<sha256>`` receipt id, or (older workers) a session its
store tags ``kanban``. Both sweeps now settle such a target without a row or a wake; the remote one records it in the
ledger with a ``swept`` reason so no later pass offers it again.
"""

import contextlib
import sqlite3
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_state import SessionDB
from tui_gateway import kanban_remote_notices as krn
from tui_gateway import kanban_unloaded as ku
from tui_gateway.kanban_remote_notices import sweep_remote_notices

from tests.tui_gateway.test_kanban_remote_notices import _hub, _ledger_row

CHAT = "20261026_110445_20914b"
RECEIPT_WORKER = "local-" + "7c" * 32
OLD_WORKER = "20261026_132706_2fca70"  # a pre-receipt worker id: only its ``kanban`` source tells
TID = "t_a989e5e0"
REF = f"hub/default/default/{TID}"


def _done_card(now):
    task = {"id": TID, "status": "done", "title": "rollout", "board": "default", "assignee": "default",
            "completed_at": now - 60,
            "completion_notice": {"event_id": 9123, "payload": {"summary": "shipped"}},
            "sessions": [{"gw": "macbook", "profile": "default", "session": sid, "rel": "shepherd"}
                         for sid in (CHAT, RECEIPT_WORKER, OLD_WORKER)]}
    return {"key": f"default/default/{TID}", "task": task}


def _store(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session(CHAT, "desktop")
    db.create_session(RECEIPT_WORKER, "kanban")
    db.create_session(OLD_WORKER, "kanban")
    return db


def _notices(db, sid):
    return [m for m in db.get_messages(sid) if m.get("display_kind") == "kanban_notice"]


class _Waker:
    grace_s = 30.0

    def __init__(self):
        self.tips = []

    def deliver(self, tip, text, metadata, task, at, *, persist, started):
        self.tips.append(tip)
        return "persisted" if persist() else "deferred"


def _swept(ledger):
    with contextlib.closing(sqlite3.connect(ledger)) as conn:
        return {row[0]: row[1] for row in conn.execute("SELECT session, reason FROM swept")}


def test_remote_sweep_settles_worker_transcripts_without_a_row_or_a_wake(tmp_path):
    now = time.time()
    replica = _hub(tmp_path, [_done_card(now)])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    waker = _Waker()
    with _store(tmp_path) as db:
        @contextlib.contextmanager
        def opener(profile, sid):
            yield (db, db.get_compression_tip(sid) or sid) if db.get_session(sid) else (None, None)

        out = sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger,
                                   now=now, waker=waker)

        assert waker.tips == [CHAT] and [item["session_id"] for item in out] == [CHAT]
        assert len(_notices(db, CHAT)) == 1
        assert _notices(db, RECEIPT_WORKER) == [] and _notices(db, OLD_WORKER) == []
        for worker in (RECEIPT_WORKER, OLD_WORKER):
            row = _ledger_row(ledger, worker, REF)
            assert row["delivered"] == krn.SETTLED and row["settled"] == "worker_transcript"
            assert row["holder_pid"] is None
        assert _swept(ledger) == {CHAT: "delivered", RECEIPT_WORKER: "worker_transcript",
                                  OLD_WORKER: "worker_transcript"}

        krn._reset_for_tests()  # a restarted backend: the ledger keeps them settled
        assert sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger,
                                    now=now, waker=waker) == []
        assert waker.tips == [CHAT]
        assert _notices(db, RECEIPT_WORKER) == [] and _notices(db, OLD_WORKER) == []


def test_passive_remote_sweep_writes_no_row_into_a_worker_transcript(tmp_path):
    """Without a waker (row-only path) the worker transcripts get nothing either; the chat still gets its row."""
    now = time.time()
    replica = _hub(tmp_path, [_done_card(now)])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    with _store(tmp_path) as db:
        @contextlib.contextmanager
        def opener(profile, sid):
            yield (db, sid) if db.get_session(sid) else (None, None)

        out = sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger, now=now)

        assert [item["session_id"] for item in out] == [CHAT]
        assert _notices(db, RECEIPT_WORKER) == [] and _notices(db, OLD_WORKER) == []
        assert _ledger_row(ledger, OLD_WORKER, REF)["settled"] == "worker_transcript"


def test_unloaded_local_sweep_drops_a_sub_on_a_worker_transcript(tmp_path):
    """The local (``tui``) sweep: a sub on a worker transcript gets no row and no wake, and the sub is removed;
    the chat on the same card is still woken."""
    db = _store(tmp_path)
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="local card", assignee="worker")
        for chat in (CHAT, OLD_WORKER):
            kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=chat)
        kb.complete_task(conn, tid, summary="shipped")
    finally:
        conn.close()

    @contextlib.contextmanager
    def opener(chat_id):
        yield (db, db.get_compression_tip(chat_id) or chat_id) if db.get_session(chat_id) else (None, None)

    calls = []

    def wake(session_id, text, metadata):
        calls.append(session_id)
        return "started"

    ku._reset_wake_budget_for_tests()
    with db:
        ku.sweep_once(set(), now=time.time() + 3600, session_db=opener, wake=wake, spawn=lambda job: job(),
                      holders=list)
        assert calls == [CHAT]
        assert _notices(db, OLD_WORKER) == []
    conn = kbc.connect()
    try:
        assert [s["chat_id"] for s in kbn.list_notify_subs(conn, tid)] == [CHAT]
    finally:
        conn.close()
