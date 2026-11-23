"""Legacy links admission must be fenced before a replacement owns a scope."""
import threading

import pytest

from hermes_cli.kanban_links_refresh import ScopeRefresher
from tests.waiting import wait_until


def test_drain_retains_fence_after_timeout_and_resumes_explicitly():
    entered, release, again = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def refresh(scope):
        calls.append(scope)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        else:
            again.set()

    owner = ScopeRefresher(refresh, 4)
    scope = ('profile', 'board')
    owner.submit(scope)
    assert entered.wait(5)
    try:
        with pytest.raises(TimeoutError):
            owner.drain(scope, timeout=0)
        owner.submit(scope)
    finally:
        release.set()
    wait_until(lambda: not owner.busy, timeout=5)
    owner.drain(scope, timeout=5)
    owner.submit(scope)
    assert not owner.busy
    assert calls == [scope]
    owner.resume(scope)
    owner.submit(scope)
    assert again.wait(5)
    wait_until(lambda: not owner.busy, timeout=5)
    assert calls == [scope, scope]


def test_drain_waits_for_active_job_without_holding_worker_lock():
    entered, release, draining, drained = (threading.Event() for _ in range(4))

    def refresh(scope):
        entered.set()
        assert release.wait(5)

    owner = ScopeRefresher(refresh, 4)
    scope = ('active',)
    owner.submit(scope)
    assert entered.wait(5)

    def drain():
        draining.set()
        owner.drain(scope, timeout=5)
        drained.set()

    thread = threading.Thread(target=drain)
    thread.start()
    try:
        assert draining.wait(5)
        assert not drained.is_set()
    finally:
        release.set()
        thread.join(5)
    assert drained.is_set()
    assert not owner.busy
    owner.submit(scope)
    assert not owner.busy


def test_drain_cancels_queued_scope_but_leaves_unrelated_work():
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def refresh(scope):
        calls.append(scope)
        if scope == ('active',):
            entered.set()
            assert release.wait(5)
        else:
            completed.set()

    owner = ScopeRefresher(refresh, 4)
    owner.submit(('active',))
    assert entered.wait(5)
    try:
        owner.submit(('cancelled',))
        owner.submit(('other',))
        owner.drain(('cancelled',), timeout=0)
        owner.submit(('cancelled',))
    finally:
        release.set()
    assert completed.wait(5)
    wait_until(lambda: not owner.busy, timeout=5)
    assert calls == [('active',), ('other',)]
