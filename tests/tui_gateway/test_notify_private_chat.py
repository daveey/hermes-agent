"""Private notification fallbacks read only the asking chat's subscription rows.

Under a GIL convoy the retained observer's 50 ms wait often fails, so each chat poller
takes a private read. That read used to fetch and group every tui subscription row on the
board (4,066 rows / 318 chats on the hub's default board, one sqlite3 step and GIL handoff
per row) only to return one chat's slice and discard the rest: private results are never
published. Live py-spy on the hub serve (t_01a20286f9f3f262, run 12017) put 160 of 224
active tui-notif-poller samples on that fetch.
"""
import contextlib
import threading

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_generation as generation
from tests.tui_gateway import test_notify_subs_gate as helpers
from tui_gateway import notification_idle as idle
from tui_gateway import notification_reader as reader

board = helpers.board
CHAT, OTHER = helpers.CHAT, helpers.OTHER


@contextlib.contextmanager
def _shared_observer_busy(path):
    """Hold the board's retained observer so the next signature read is private."""
    ready, release = threading.Event(), threading.Event()

    def hold():
        with reader.reading(path):
            ready.set()
            release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(5)
        yield
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()


def _traced_private(monkeypatch):
    original = generation.open_ro
    statements: list = []

    def traced(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(generation, "open_ro", traced)
    return statements


def _many_chats(tasks, n=40):
    with contextlib.closing(kbc.connect()) as conn:
        for i in range(n):
            kbn.add_notify_sub(conn, task_id=tasks[i % len(tasks)], platform="tui",
                               chat_id=f"20261113_1300{i:02d}_bulk{i:02d}")


def test_private_fallback_reads_only_the_asking_chat(board, monkeypatch):
    path, tasks = board
    _many_chats(tasks)
    shared = {chat: idle.board_signature(path, chat) for chat in (CHAT, OTHER, "absent-chat")}
    held = idle._SIGS[str(path)]
    statements = _traced_private(monkeypatch)
    with _shared_observer_busy(path):
        private = {chat: idle.board_signature(path, chat) for chat in (CHAT, OTHER, "absent-chat")}
    assert private == shared  # byte-identical signatures, including the empty slice
    assert idle._SIGS[str(path)] is held  # still never published
    assert not any(s.startswith(idle._SUBS_SQL) for s in statements), statements
    chat_reads = [s for s in statements if "FROM kanban_notify_subs" in s and "chat_id = " in s]
    assert len(chat_reads) == 3, statements


def test_private_fallback_sees_fresh_rows_for_its_chat(board, monkeypatch):
    from tests.tui_gateway.test_notify_revision import writer
    path, _tasks = board
    before = idle.board_signature(path, CHAT)
    _traced_private(monkeypatch)
    with _shared_observer_busy(path):
        with writer(path) as conn:
            conn.execute("UPDATE kanban_notify_subs SET last_event_id = 77, inflight_from = 5 WHERE chat_id = ?",
                         (CHAT,))
        fresh = idle.board_signature(path, CHAT)
    assert fresh != before
    assert {row[4] for row in fresh[2]} == {77} and {row[5] for row in fresh[2]} == {5}
    # the retained observer then reads the same rows
    assert idle.board_signature(path, CHAT) == fresh


def test_private_fallback_platform_match_is_case_insensitive(board):
    path, tasks = board
    with contextlib.closing(kbc.connect()) as conn:
        kbn.add_notify_sub(conn, task_id=tasks[1], platform="TUI", chat_id=OTHER)
        kbn.add_notify_sub(conn, task_id=tasks[2], platform="telegram", chat_id=OTHER)
    shared = idle.board_signature(path, OTHER)
    with _shared_observer_busy(path):
        assert idle.board_signature(path, OTHER) == shared


def test_unscoped_private_fallback_keeps_every_chat(board):
    """``chat_id=None`` (session-list stamping) still needs the whole map."""
    path, _tasks = board
    shared = idle.board_signature(path)
    with _shared_observer_busy(path):
        assert idle.board_signature(path) == shared
    assert set(shared[2]) == {CHAT, OTHER}


def test_retained_reader_still_publishes_every_chat(board):
    path, _tasks = board
    idle.board_signature(path, CHAT)
    assert set(idle._SIGS[str(path)][4]) == {CHAT, OTHER}
