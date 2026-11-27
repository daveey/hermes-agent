"""Subscription reads behind ``board_signature`` hand their rows over in one packed row.

CPython's sqlite3 releases the GIL on every stepped row. A changed subscription revision makes the
retained observer re-read every tui subscription row on the board (~4k on the hub), and under the
serve's convoy each of those handoffs waited for the GIL: after the private-read cut, live py-spy
still put 490 of 714 active ``_notify_content`` samples on that fetch (t_01a20286f9f3f262).
"""
import contextlib

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_generation as generation
from tests.tui_gateway import test_notify_private_chat as private
from tests.tui_gateway import test_notify_subs_gate as helpers
from tui_gateway import notification_idle as idle

board = helpers.board
CHAT, OTHER = helpers.CHAT, helpers.OTHER
N_CHATS = 40


def _counted_rows(monkeypatch):
    """Count every row sqlite3 hands to Python on connections the notification readers open."""
    original = generation.open_ro
    handed = {"rows": 0}

    def counted(*args, **kwargs):
        conn = original(*args, **kwargs)
        inner = conn.row_factory

        def factory(cursor, row):
            handed["rows"] += 1
            return inner(cursor, row) if inner is not None else row

        conn.row_factory = factory
        return conn

    monkeypatch.setattr(generation, "open_ro", counted)
    return handed


def _bulk(tasks):
    with contextlib.closing(kbc.connect()) as conn:
        for i in range(N_CHATS):
            kbn.add_notify_sub(conn, task_id=tasks[i % len(tasks)], platform="tui",
                               chat_id=f"20261127_2200{i:02d}_bulk{i:02d}")


def _fresh_reader():
    idle.reset_board_signatures()


def test_retained_full_subscription_read_is_one_handed_row(board, monkeypatch):
    path, tasks = board
    _bulk(tasks)
    _fresh_reader()
    expected = idle.board_signature(path)  # unpatched: the reference answer
    _fresh_reader()
    handed = _counted_rows(monkeypatch)
    got = idle.board_signature(path)
    assert got == expected
    assert len(got[2]) == N_CHATS + 2
    # sqlite_master (2), data_version, the revision probes and MAX(id)s: a handful, never one per row
    assert handed["rows"] < N_CHATS, handed


def test_private_chat_read_is_one_handed_row(board, monkeypatch):
    path, _tasks = board
    with contextlib.closing(kbc.connect()) as conn:
        for i in range(N_CHATS):
            kbn.add_notify_sub(conn, task_id=f"t_bulk{i:03d}", platform="tui", chat_id=CHAT)
    shared = idle.board_signature(path, CHAT)
    assert len(shared[2]) == N_CHATS + 3
    handed = _counted_rows(monkeypatch)
    with private._shared_observer_busy(path):
        got = idle.board_signature(path, CHAT)
    assert got == shared
    assert handed["rows"] < N_CHATS, handed


def test_packed_rows_keep_types_nulls_and_order(board):
    from tests.tui_gateway.test_notify_revision import writer
    path, _tasks = board
    with writer(path) as conn:
        conn.execute("UPDATE kanban_notify_subs SET last_event_id = 9007199254740993, inflight_from = NULL, "
                     "thread_id = '' WHERE chat_id = ?", (OTHER,))
    sig = idle.board_signature(path)
    rows = sig[2][OTHER]
    assert [r[4] for r in rows] == [9007199254740993] and rows[0][5] is None
    assert type(rows[0][4]) is int
    for chat, subs in sig[2].items():
        assert list(subs) == sorted(subs, key=lambda r: (r[1], r[2], r[3]))
    assert list(sig[2]) == sorted(sig[2])


def test_a_value_json_cannot_carry_reads_plainly(board):
    from tests.tui_gateway.test_notify_revision import writer
    path, _tasks = board
    with writer(path) as conn:
        conn.execute("UPDATE kanban_notify_subs SET thread_id = X'00ff' WHERE chat_id = ?", (OTHER,))
    rows = idle.board_signature(path)[2][OTHER]
    assert rows[0][3] == b"\x00\xff"
    with idle_conn(path) as conn:
        assert idle._subs_rows(conn, idle._SUBS_SQL) == [tuple(r) for r in conn.execute(idle._SUBS_SQL)]


@contextlib.contextmanager
def idle_conn(path):
    import sqlite3
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()
