"""[daveey] Card J (t_01a1d563324d7e8e): the kanban poll's per-commit read keys the tui subscription rows on what
they are made of.

``notification_idle.board_signature`` runs once per board commit for every chat poller in the process. The hub's
main board moved ``data_version`` 92 times a minute and its activity revision 9 times (heartbeats, claims and run
stamps are bookkeeping), so re-reading every tui subscription row (2.1k rows, 3 ms) on each commit was most of the
serve's poll. The rows are now reused while the independent subscription revision (including the in-flight mark)
is unchanged. The annotation's activity revision is global, not table-local. Every input change is still seen
on the next call, and a chat with a claim in flight is polled every tick, so a claim that goes stale on the clock
is recovered with no other change.
"""
import contextlib
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tui_gateway import notification_idle as idle

CHAT = "20261113_120000_abcdef"
OTHER = "20261113_120000_fedcba"


@pytest.fixture()
def board():
    idle.reset_board_signatures()
    with contextlib.closing(kbc.connect()) as conn:
        tasks = [kb.create_task(conn, title=f"card {n}", assignee="default") for n in range(3)]
        for tid in tasks:
            kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=CHAT)
        kbn.add_notify_sub(conn, task_id=tasks[0], platform="tui", chat_id=OTHER)
    yield kb.kanban_db_path(board=kb.DEFAULT_BOARD), tasks
    idle.reset_board_signatures()


def _write(sql: str, *args) -> None:
    with contextlib.closing(kbc.connect()) as conn:
        with kb.write_txn(conn):
            conn.execute(sql, args)


def _bookkeeping(tid: str, n: int) -> None:
    """A commit no chat poll reads: a claim clock on a card (``BOOKKEEPING_COLUMNS``)."""
    _write("UPDATE tasks SET claim_expires = ? WHERE id = ?", 10_000 + n, tid)


@contextlib.contextmanager
def _subs_reads(path):
    """Full tui subscription reads on the retained notification observer."""
    from tui_gateway.notification_reader import reading
    seen: list = []
    with reading(path) as (conn, _retained):
        conn.set_trace_callback(lambda sql: seen.append(sql) if "last_event_id, inflight_from FROM" in sql else None)
    try:
        yield seen
    finally:
        with contextlib.suppress(Exception), reading(path) as (conn, _retained):
            conn.set_trace_callback(None)


def test_a_bookkeeping_commit_does_not_reread_the_subscription_rows(board):
    path, tasks = board
    before = idle.board_signature(path, CHAT)
    with _subs_reads(path) as reads:
        for n in range(5):
            _bookkeeping(tasks[1], n)
            assert idle.board_signature(path, CHAT) == before
    assert reads == [], f"{len(reads)} full subscription reads for 5 bookkeeping commits"


def test_a_cursor_move_is_seen_on_the_next_call(board):
    path, tasks = board
    before = idle.board_signature(path, CHAT)
    other = idle.board_signature(path, OTHER)
    _write("UPDATE kanban_notify_subs SET last_event_id = last_event_id + 7 WHERE task_id = ? AND chat_id = ?",
           tasks[2], CHAT)
    assert idle.board_signature(path, CHAT) != before
    assert idle.board_signature(path, OTHER) == other, "another chat's rows did not move"


def test_a_new_and_a_removed_subscription_are_seen(board):
    path, tasks = board
    before = idle.board_signature(path, OTHER)
    with contextlib.closing(kbc.connect()) as conn:
        kbn.add_notify_sub(conn, task_id=tasks[1], platform="tui", chat_id=OTHER)
    assert idle.board_signature(path, OTHER) != before
    _write("DELETE FROM kanban_notify_subs WHERE task_id = ? AND chat_id = ?", tasks[1], OTHER)
    assert idle.board_signature(path, OTHER) == before


def test_an_in_flight_mark_alone_is_seen(board):
    """inflight_from moves the notify counter even though annotation bookkeeping excludes it."""
    path, tasks = board
    before = idle.board_signature(path, CHAT)
    _write("UPDATE kanban_notify_subs SET inflight_from = 0, inflight_pid = 4242 WHERE task_id = ? AND chat_id = ?",
           tasks[0], CHAT)
    assert idle.board_signature(path, CHAT) != before
    _write("UPDATE kanban_notify_subs SET inflight_from = NULL, inflight_pid = NULL WHERE task_id = ? AND chat_id = ?",
           tasks[0], CHAT)
    assert idle.board_signature(path, CHAT) == before, "a released claim is seen with no other change"


def test_without_the_counter_triggers_the_rows_are_read_on_every_commit(board):
    """An older writer that dropped the triggers: the revision cannot vouch for the rows."""
    from hermes_cli.kanban_notify_revision import TRIGGERS
    path, tasks = board
    with contextlib.closing(sqlite3.connect(path)) as raw:  # a plain writer: nothing reinstalls the triggers
        for name in TRIGGERS:
            raw.execute(f"DROP TRIGGER {name}")
        raw.commit()
    before = idle.board_signature(path, CHAT)
    with _subs_reads(path) as reads:
        with contextlib.closing(sqlite3.connect(path)) as raw:
            raw.execute("UPDATE tasks SET claim_expires = 1 WHERE id = ?", (tasks[1],))
            raw.commit()
        assert idle.board_signature(path, CHAT) == before
    assert reads, "no coverage: the rows are read on each commit"
    with contextlib.closing(sqlite3.connect(path)) as raw:
        raw.execute("UPDATE kanban_notify_subs SET last_event_id = 99 WHERE task_id = ? AND chat_id = ?",
                    (tasks[2], CHAT))
        raw.commit()
    assert idle.board_signature(path, CHAT) != before


def test_a_new_deliverable_event_is_seen(board):
    path, tasks = board
    before = idle.board_signature(path, CHAT)
    with contextlib.closing(kbc.connect()) as conn:
        with kb.write_txn(conn):
            kb._append_event(conn, tasks[0], "completed", {})
    assert idle.board_signature(path, CHAT) != before


def test_a_claim_in_flight_is_polled_every_tick_with_no_commit(board, monkeypatch):
    """The due-time input: an in-flight claim goes stale on the clock (``recover_orphaned_notify_claims`` runs in
    the poll), not on a commit, so the chat's board is never skipped while its claim is out."""
    from tui_gateway import server
    _path, tasks = board
    _write("UPDATE kanban_notify_subs SET inflight_from = 0, inflight_pid = 4242, inflight_at = ? "
           "WHERE task_id = ? AND chat_id = ?", int(time.time()) - 3600, tasks[0], CHAT)
    polls = []
    monkeypatch.setattr(server, "_kb_poll_board_now", lambda *a, **k: polls.append(a) or [])
    seen: dict = {}
    for _ in range(3):
        server._kb_poll_board(kb, kb.DEFAULT_BOARD, CHAT, None, seen)
    assert len(polls) == 3
    _write("UPDATE kanban_notify_subs SET inflight_from = NULL, inflight_pid = NULL, inflight_at = NULL "
           "WHERE task_id = ? AND chat_id = ?", tasks[0], CHAT)
    for _ in range(3):
        server._kb_poll_board(kb, kb.DEFAULT_BOARD, CHAT, None, seen)
    assert len(polls) == 4, "once released and proven quiet, the board is skipped"


# -- replay benchmark ---------------------------------------------------------------------------------------------
# Design §3 card J: serve change_watcher + notification poller 2.0 -> 0.7 (% of one core). This read's share is
# held to 0.3: the hub's stream (main board, 60 s sampled at 0.5 s: data_version moved 92 times, the activity
# revision 9) over its 2.1k tui subscriptions in 136 chats, with 10 live chats each probing every 5 s. A commit is
# read once per process (the first chat to look after it); the other probes reuse it.

SUBS, CHATS, LIVE_CHATS, POLL_S = 2100, 136, 10, 5.0
COMMITS_PER_S = 92 / 60
REVISION_EVERY = round(92 / 9)
BUDGET_CORE = 0.003


def test_replay_budget_board_signature(board):
    path, tasks = board
    now = int(time.time())
    with contextlib.closing(kbc.connect()) as conn:
        with kb.write_txn(conn):
            conn.executemany(
                "INSERT INTO kanban_notify_subs(task_id, platform, chat_id, thread_id, created_at, last_event_id) "
                "VALUES (?, 'tui', ?, '', ?, 0)", [(f"t_bench_{n}", f"chat-{n % CHATS}", now) for n in range(SUBS)])
    chats = [f"chat-{n}" for n in range(LIVE_CHATS)]
    for chat in chats:
        idle.board_signature(path, chat)
    commits = 40
    first = rest = 0.0
    for n in range(commits):
        if n % REVISION_EVERY == 0:
            _write("UPDATE kanban_notify_subs SET last_event_id = ? WHERE task_id = 't_bench_0'", n + 1)
        else:
            _bookkeeping(tasks[1], n)
        t0 = time.thread_time()
        idle.board_signature(path, chats[0])  # the first probe after a commit reads it
        t1 = time.thread_time()
        for chat in chats[1:]:
            idle.board_signature(path, chat)
        first, rest = first + t1 - t0, rest + time.thread_time() - t1
    reread = first / commits
    warm = rest / commits / (LIVE_CHATS - 1)
    per_s = reread * min(COMMITS_PER_S, LIVE_CHATS / POLL_S) + warm * LIVE_CHATS / POLL_S
    assert per_s <= BUDGET_CORE, (f"{reread * 1000:.2f} ms per commit, {warm * 1000:.3f} ms per probe = "
                                  f"{per_s:.3%} of a core")
