"""[daveey] An idle chat poll takes no board write lock.

Every chat's poller runs ``_kb_poll_board_now`` every few seconds. Opening ``BEGIN IMMEDIATE`` for
``recover_orphaned_notify_claims`` and ``claim_unseen_events_for_sub`` with nothing to claim or recover queued
those pollers for the kanban.db write lock (108 thread-seconds of 90 s on hermes-hub, in
``_execute_boundary_with_retry``). A read-only probe decides first; claiming stays inside the write transaction.
"""
import contextlib
import os
import time
from types import SimpleNamespace

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn

KEY = "20261112_040336_f74a3e"


def _task() -> str:
    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="Idle card", assignee="default")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=KEY)
        return tid


def _event(tid: str, kind: str) -> int:
    with contextlib.closing(kbc.connect()) as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, kind, {})
        return int(conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0])


def _hold(tid: str, *, inflight_from: int, last_event_id: int, pid: int) -> None:
    with contextlib.closing(kbc.connect()) as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE kanban_notify_subs SET inflight_from = ?, last_event_id = ?, inflight_pid = ?, "
                         "inflight_start = NULL, inflight_at = ? WHERE task_id = ?",
                         (inflight_from, last_event_id, pid, int(time.time()), tid))


def _sub(tid: str) -> dict:
    with contextlib.closing(kbc.connect()) as conn:
        return dict(conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id = ?", (tid,)).fetchone())


def _poll(claims=None):
    """(texts, write transactions the poll's board connection opened)."""
    begins = []

    def connect(*args, **kwargs):
        conn = kbc.connect(*args, **kwargs)
        conn.set_trace_callback(lambda sql: begins.append(sql) if sql.upper().startswith("BEGIN") else None)
        return conn

    from tui_gateway import server  # the split module's functions run against server.py's globals

    texts = server._kb_poll_board_now(kb, SimpleNamespace(connect=connect), kbn, kb.DEFAULT_BOARD, KEY, claims)
    return texts, begins


def test_an_idle_poll_opens_no_write_transaction():
    tid = _task()
    _poll([])  # settle anything the creation left to claim
    texts, begins = _poll([])
    assert texts == [] and begins == []
    _event(tid, "blocked")
    texts, begins = _poll([])
    assert len(texts) == 1 and begins  # a new event is claimed, inside the write transaction
    assert _sub(tid)["inflight_pid"] == os.getpid()
    texts, begins = _poll([])  # our own claim in flight, nothing new: still idle
    assert texts == [] and begins == []


def test_a_live_foreign_claim_is_left_alone_without_the_write_lock(monkeypatch):
    tid = _task()
    _poll([])
    before = _sub(tid)["last_event_id"]
    blocked = _event(tid, "blocked")
    _hold(tid, inflight_from=before, last_event_id=blocked, pid=os.getppid())  # another live process holds it
    _event(tid, "unblocked")  # unseen, but its holder delivers it
    texts, begins = _poll([])
    assert texts == [] and begins == []
    assert _sub(tid)["inflight_pid"] == os.getppid()


def test_a_dead_holders_claim_is_still_recovered_and_delivered(monkeypatch):
    tid = _task()
    _poll([])
    before = _sub(tid)["last_event_id"]
    blocked = _event(tid, "blocked")
    _hold(tid, inflight_from=before, last_event_id=blocked, pid=os.getppid())
    monkeypatch.setattr(kbn, "_holder_alive", lambda row: int(row["inflight_pid"]) != os.getppid())
    texts, begins = _poll([])
    assert len(texts) == 1 and begins
    assert _sub(tid)["inflight_pid"] == os.getpid()


def test_unseen_events_of_silent_kinds_only_take_no_write_lock():
    tid = _task()
    _poll([])
    with contextlib.closing(kbc.connect()) as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "commented", {})  # not a notify kind
    texts, begins = _poll([])
    assert texts == [] and begins == []


def test_loaded_poll_only_walks_pending_kind_and_inflight_candidates(monkeypatch):
    """A global board hint must not replay the chat's entire historical subscription set."""
    tid = _task()
    _poll([])
    with contextlib.closing(kbc.connect()) as conn:
        with kb.write_txn(conn):
            conn.executemany(
                "INSERT INTO kanban_notify_subs(task_id, platform, chat_id, thread_id, created_at, last_event_id) "
                "VALUES (?, 'tui', ?, '', 0, 0)",
                [(f"quiet-{n}", KEY) for n in range(300)],
            )
            for n in range(300):
                kb._append_event(conn, f"quiet-{n}", "commented", {})
    listed, claimed = [], []
    real_list, real_claim = kbn.list_notify_subs, kbn.claim_unseen_events_for_sub

    def listing(*args, **kwargs):
        rows = real_list(*args, **kwargs)
        listed.append([row["task_id"] for row in rows])
        return rows

    def claiming(*args, **kwargs):
        claimed.append(kwargs["task_id"])
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(kbn, "list_notify_subs", listing)
    monkeypatch.setattr(kbn, "claim_unseen_events_for_sub", claiming)
    _event(tid, "blocked")
    texts, _ = _poll([])
    assert len(texts) == 1 and tid in texts[0]
    assert listed == [[tid]], "quiet/silent-only subscriptions must stay inside SQLite"
    assert claimed == [tid]
    # Even with no unseen event, own in-flight custody remains eligible until ACK.
    listed.clear()
    claimed.clear()
    assert _poll([])[0] == []
    assert listed == [[tid]] and claimed == [tid]


def test_loaded_poll_keeps_unseen_silent_notify_kinds_eligible(monkeypatch):
    """Unblocked has no text but must still advance and settle the cursor."""
    tid = _task()
    _poll([])
    eid = _event(tid, "unblocked")
    assert _poll([])[0] == []
    sub = _sub(tid)
    assert sub["last_event_id"] == eid
    assert sub["inflight_from"] is None


def test_an_unsubscribed_row_claims_nothing_without_the_write_lock():
    tid = _task()
    _event(tid, "blocked")
    begins = []
    with contextlib.closing(kbc.connect()) as conn:
        conn.set_trace_callback(lambda sql: begins.append(sql) if sql.upper().startswith("BEGIN") else None)
        got = kbn.claim_unseen_events_for_sub(conn, task_id=tid, platform="tui", chat_id="someone-else",
                                              durable=True)
    assert got == (0, 0, []) and begins == []
