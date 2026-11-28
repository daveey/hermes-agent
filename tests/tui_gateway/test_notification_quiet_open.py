"""Quiet chats must not acquire writable boards on another chat's event."""
import contextlib
import threading

from hermes_constants import get_hermes_home
from tui_gateway import kanban_consumer_health as health

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tui_gateway import notification_idle as idle
from tui_gateway import server

CHAT = '20261113_120000_abcdef'


@pytest.mark.parametrize('use_seen', [False, True])
def test_missing_board_is_not_created(use_seen):
    path = kb.kanban_db_path()
    assert not path.exists()
    assert server._kb_poll_board(kb, kb.DEFAULT_BOARD, CHAT, [], {} if use_seen else None) == []
    assert not path.exists(), 'a chat poll must not recreate a missing board'


@pytest.mark.parametrize('use_seen', [False, True])
def test_quiet_read_refreshes_registered_consumer_health(board, use_seen):
    path, _own, other = board
    home = get_hermes_home()
    stop, ready = threading.Event(), threading.Event()
    errors = []

    def run():
        try:
            seen = {} if use_seen else None
            for _ in range(2):
                with contextlib.closing(kbc.connect()) as conn, kb.write_txn(conn):
                    kb._append_event(conn, other, 'completed', {})
                assert server._kb_poll_board(kb, kb.DEFAULT_BOARD, CHAT, [], seen) == []
                assert health.available(home, CHAT, path)
        except BaseException as exc:
            errors.append(exc)
        finally:
            ready.set()
            stop.wait(3)

    thread = threading.Thread(target=run)
    health.register({'session_key': CHAT, 'profile_home': str(home)}, stop, thread)
    thread.start()
    try:
        assert ready.wait(3)
        assert not errors, errors
        assert health.available(home, CHAT, path)
    finally:
        stop.set()
        thread.join(3)


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


@pytest.mark.parametrize('failure', ['connect', 'subscriptions', 'missing'])
def test_failed_poll_never_seeds_quiet_health_cache(board, monkeypatch, failure):
    path, _own, _other = board
    polled = []
    attempts = []
    seen = {}
    monkeypatch.setattr(health, 'polled', lambda *args: polled.append(args))
    monkeypatch.setattr(server, '_kb_board_file_sig', lambda *args: 'stable')
    # A later quiet observation cannot retroactively certify a failed poll.
    quiet = [False]
    monkeypatch.setattr(idle, 'board_quiet', lambda *args: quiet[0])

    def unavailable(*args, **kwargs):
        attempts.append(1)
        quiet[0] = True
        raise OSError('unreadable board')

    if failure == 'missing':
        monkeypatch.setattr(kb, 'kanban_db_path', lambda **kwargs: path / 'missing.db')
        quiet[0] = True
    else:
        monkeypatch.setattr(kbc if failure == 'connect' else kbn,
                            'connect' if failure == 'connect' else 'list_notify_subs', unavailable)
    for _ in range(2):
        assert server._kb_poll_board(kb, kb.DEFAULT_BOARD, CHAT, [], seen) == []
        assert seen == {}, 'failed polls must not authorize the unchanged-board shortcut'
        assert polled == [], 'failed polls must not certify consumer health'
        quiet[0] = failure == 'missing'
    if failure != 'missing':
        assert attempts == [1, 1], 'an unchanged signature must still retry the failed read'
