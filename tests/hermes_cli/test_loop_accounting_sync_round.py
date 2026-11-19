"""A perpetual executor item must publish completed rounds before it returns."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from hermes_cli import kanban_sync_cpu as cpu
from hermes_cli import kanban_sync_timing as timing
from hermes_cli import loop_accounting as la


@pytest.fixture(autouse=True)
def accounting():
    la.stop()
    la.reset()
    yield
    la.uninstrument()
    la.reset()


def burn(seconds):
    end = time.thread_time() + seconds
    while time.thread_time() < end:
        pass


@pytest.mark.parametrize('board_kind', ['scope', 'pass'])
def test_running_executor_publishes_round_residual_once(monkeypatch, board_kind):
    monkeypatch.setattr(timing._DIAG, 'board_start', lambda *a: None)
    monkeypatch.setattr(timing._DIAG, 'board_end', lambda *a: None)
    la.instrument_executors()
    published = threading.Event()
    release = threading.Event()
    rounds_done = 0

    def keep_running():
        if rounds_done:
            published.set()
            assert release.wait(10)
            return False
        return True

    def job():
        nonlocal rounds_done
        with cpu.iterations(keep_running) as rounds:
            for _ in rounds:
                burn(0.03)
                if board_kind == 'scope':
                    with la.Scope('sync.board/test'):
                        burn(0.03)
                else:
                    results = []
                    token = timing.board_start('test', results)
                    try:
                        burn(0.03)
                    finally:
                        timing.board_end(token, results)
                rounds_done += 1

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(job)
        try:
            assert published.wait(10)
            assert not future.done()
            snap = la.snapshot()
            rows = la.counters()
            assert rows['sync.round/loop']['runs'] == 1
            assert 0.025 <= rows['sync.round/loop']['cpu_s'] < 0.055
            assert 0.025 <= rows['sync.board/test']['cpu_s'] < 0.055
            assert snap['process']['accounted_cpu_s'] == pytest.approx(
                rows['sync.round/loop']['cpu_s'] + rows['sync.board/test']['cpu_s'], abs=0.005)
        finally:
            release.set()
        future.result(timeout=10)
    rows = la.counters()
    name = la._callable_name(job)
    assert rows['thread/' + name]['cpu_s'] < 0.015
    assert rows['executor_item/' + name]['cpu_s'] < 0.015


def test_success_in_ambient_except_does_not_book_error():
    def job():
        return 42
    try:
        raise KeyError('ambient')
    except KeyError:
        assert la.scoped_call('thread', job)() == 42
    row = la.counters()['thread/' + la._callable_name(job)]
    assert row['errors'] == 0


def test_operation_failure_books_its_own_error():
    failure = ValueError('own error')
    def job():
        raise failure
    with pytest.raises(ValueError) as caught:
        la.scoped_call('thread', job)()
    assert caught.value is failure
    row = la.counters()['thread/' + la._callable_name(job)]
    assert row['errors'] == 1
    assert row['error_kinds'] == {'ValueError': 1}


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('raises', [False, True])
def test_round_toggle_and_exception_cleanup(monkeypatch, enabled, raises):
    monkeypatch.setattr(la, 'scopes_enabled', lambda: enabled)
    failure = ValueError('round failure')
    def run():
        with cpu.iterations(lambda: True) as rounds:
            next(rounds)
            if raises:
                raise failure
    if raises:
        with pytest.raises(ValueError) as caught:
            run()
        assert caught.value is failure
    else:
        run()
    rows = la.counters()
    assert ('sync.round/loop' in rows) is enabled
    if enabled:
        assert rows['sync.round/loop']['runs'] == 1
        assert rows['sync.round/loop']['errors'] == int(raises)
    assert cpu._local.frames == ()
