"""Quiet chats must not acquire writable boards on another chat's event."""
import contextlib

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tui_gateway import notification_idle as idle
from tui_gateway import server

CHAT = '20261113_120000_abcdef'


@pytest.fixture
def board():
    idle.reset_board_signatures()
    with contextlib.closing(kbc.connect()) as conn:
        own = kb.create_task(conn, title='own', assignee='default')
        other = kb.create_task(conn, title='other', assignee='default')
        kbn.add_notify_sub(conn, task_id=own, platform='tui', chat_id=CHAT)
    yield kb.kanban_db_path(), own, other
    idle.reset_board_signatures()


@pytest.mark.parametrize('use_seen', [False, True])
def test_unrelated_notify_events_do_not_open_a_quiet_chat_writable(board, monkeypatch, use_seen):
    _path, own, other = board
    connect = kbc.connect
    opens = []
    monkeypatch.setattr(kbc, 'connect', lambda *a, **kw: opens.append(1) or connect(*a, **kw))
    seen = {} if use_seen else None
    for _ in range(3):
        with contextlib.closing(connect()) as conn, kb.write_txn(conn):
            kb._append_event(conn, other, 'completed', {})
        assert server._kb_poll_board(kb, kb.DEFAULT_BOARD, CHAT, [], seen) == []
    assert opens == [], 'global newest-event hints must not open quiet chats writable'
    with contextlib.closing(connect()) as conn, kb.write_txn(conn):
        kb._append_event(conn, own, 'completed', {})
    claims = []
    assert server._kb_poll_board(kb, kb.DEFAULT_BOARD, CHAT, claims, seen)
    assert len(opens) == 1
    assert claims


def test_quiet_probe_uses_case_insensitive_platform(board):
    path, own, _other = board
    with contextlib.closing(kbc.connect()) as conn, kb.write_txn(conn):
        conn.execute("UPDATE kanban_notify_subs SET platform='TUI' WHERE task_id=?", (own,))
        kb._append_event(conn, own, 'completed', {})
    assert not idle.board_quiet(path, CHAT)


def test_failed_quiet_probe_still_enters_claim_path(board, monkeypatch):
    from tui_gateway import notification_reader
    _path, own, _other = board
    with contextlib.closing(kbc.connect()) as conn, kb.write_txn(conn):
        kb._append_event(conn, own, 'completed', {})

    def unavailable(*args, **kwargs):
        raise OSError('temporary observer failure')

    monkeypatch.setattr(notification_reader, 'reading', unavailable)
    assert server._kb_poll_board(kb, kb.DEFAULT_BOARD, CHAT, [], {})


def test_inflight_without_new_event_still_opens_each_tick(board, monkeypatch):
    _path, own, _other = board
    with contextlib.closing(kbc.connect()) as conn, kb.write_txn(conn):
        conn.execute('UPDATE kanban_notify_subs SET inflight_from=0 WHERE task_id=?', (own,))
    connect = kbc.connect
    opens = []
    monkeypatch.setattr(kbc, 'connect', lambda *a, **kw: opens.append(1) or connect(*a, **kw))
    monkeypatch.setattr(kbn, 'recover_orphaned_notify_claims', lambda *a, **kw: None)
    monkeypatch.setattr(kbn, 'claim_unseen_events_for_sub', lambda *a, **kw: (0, 0, []))
    for _ in range(3):
        assert server._kb_poll_board(kb, kb.DEFAULT_BOARD, CHAT, [], {}) == []
    assert len(opens) == 3


def test_event_committed_after_empty_probe_is_delivered_next_tick(board, monkeypatch):
    _path, own, _other = board
    quiet = idle.board_quiet
    calls = []

    def racing(path, chat):
        result = quiet(path, chat)
        if not calls:
            calls.append(1)
            with contextlib.closing(kbc.connect()) as conn, kb.write_txn(conn):
                kb._append_event(conn, own, 'completed', {})
        return result

    monkeypatch.setattr(idle, 'board_quiet', racing)
    seen = {}
    assert server._kb_poll_board(kb, kb.DEFAULT_BOARD, CHAT, [], seen) == []
    assert server._kb_poll_board(kb, kb.DEFAULT_BOARD, CHAT, [], seen)
