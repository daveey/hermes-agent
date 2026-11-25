"""Concurrent request refreshes share one board read, never a connection."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_external_activity as activity


@pytest.fixture(autouse=True)
def fresh():
    activity.reset_for_tests()
    yield
    activity.reset_for_tests()


@pytest.mark.parametrize('reason', ['cold', 'changed', 'clock'])
def test_overlapping_requests_recompute_once(monkeypatch, reason):
    entered = threading.Event()
    release = threading.Event()
    duplicate = threading.Event()
    started = threading.Event()
    opens = []
    signature = ['first']
    wall = [100.0]
    hold = [False]
    result = {'s': {'counts': [1, 0, 0], 'cards': []}}

    def open_ro(path):
        conn = Mock()
        opens.append(conn)
        return conn

    def read(conn, pairs, **kwargs):
        if hold[0]:
            if entered.is_set():
                duplicate.set()
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(activity, 'session_external', read)
    monkeypatch.setattr(activity, '_clocked', lambda conn, pairs: reason == 'clock')

    def request():
        return activity._board_external('board', open_ro, lambda conn: [('s', 't')],
                                        lambda path: signature[0], wall[0])

    if reason != 'cold':
        assert request() == result
        opens.clear()
        if reason == 'changed':
            signature[0] = 'second'
        else:
            wall[0] += activity._TTL_S + 1
    hold[0] = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request)
        assert entered.wait(5)

        def second_request():
            started.set()
            return request()

        second = pool.submit(second_request)
        try:
            assert started.wait(5)
            duplicate.wait(0.2)
        finally:
            release.set()
        assert first.result(timeout=5) == second.result(timeout=5) == result
    assert len(opens) == 1, 'one changed-board capture must serve overlapping requests'
    opens[0].close.assert_called_once()


def test_failed_open_does_not_poison_next_request(monkeypatch):
    conn = Mock()
    opened = Mock(side_effect=[sqlite3.OperationalError('busy'), conn])
    monkeypatch.setattr(activity, '_clocked', lambda conn, pairs: False)
    read = Mock(return_value={'s': {'counts': [1, 0, 0], 'cards': []}})
    monkeypatch.setattr(activity, 'session_external', read)

    def request():
        return activity._board_external('board', opened, lambda conn: [('s', 't')], lambda path: 'sig', 100)

    assert request() == {}
    assert request() == read.return_value
    assert request() == read.return_value
    assert opened.call_count == 2
    assert read.call_count == 1
    conn.close.assert_called_once()


def test_board_locks_are_independent_and_retired_on_exception():
    from hermes_cli import kanban_external_refresh as refresh

    with refresh.board_refresh('first'):
        with ThreadPoolExecutor(max_workers=1) as pool:
            def other():
                with refresh.board_refresh('second'):
                    return True
            assert pool.submit(other).result(timeout=5)
    assert not refresh._active
    with pytest.raises(RuntimeError), refresh.board_refresh('first'):
        raise RuntimeError('capture failed')
    assert not refresh._active


def test_waiter_rechecks_signature_after_prior_capture(monkeypatch):
    from hermes_cli import kanban_external_refresh as refresh

    started = threading.Event()
    signal = ['old']
    seen = []
    conn = Mock()
    monkeypatch.setattr(activity, '_clocked', lambda conn, pairs: False)
    monkeypatch.setattr(activity, 'session_external', lambda *args, **kw: {'new': {}})
    activity._boards['board'] = ('old', False, 100, {'old': {}})

    def signature(path):
        seen.append(signal[0])
        return signal[0]

    def request():
        started.set()
        return activity._board_external('board', lambda path: conn, lambda conn: [('s', 't')], signature, 100)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with refresh.board_refresh('board'):
            pending = pool.submit(request)
            assert started.wait(5)
            signal[0] = 'new'
        assert pending.result(timeout=5) == {'new': {}}
    assert seen == ['new']
    assert not refresh._active
