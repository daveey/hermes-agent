"""Individual housekeeping passes and async completion retain drain custody."""
from concurrent.futures import Future, TimeoutError
from types import SimpleNamespace

import pytest

from hermes_cli import worker_drain_fence as fence


@pytest.fixture
def custody(monkeypatch):
    events = []
    lease = SimpleNamespace(closed=False)

    def close():
        assert not lease.closed
        lease.closed = True
        events.append('close')

    lease.close = close
    monkeypatch.setattr(fence, 'admit', lambda purpose: (events.append('admit') or lease))
    return lease, events


def test_housekeeping_holds_through_cleanup(custody):
    from gateway.run import _housekeeping_chore
    lease, events = custody

    def chore():
        assert not lease.closed and fence._authority.get() is lease
        events.append('chore')
        raise ValueError('logged failure')

    _housekeeping_chore('fixture', chore)
    assert events == ['admit', 'chore', 'close']


def test_housekeeping_does_not_call_when_admission_refused(monkeypatch):
    from gateway.run import _housekeeping_chore

    def refuse(purpose):
        raise fence.DrainRefused('draining')

    monkeypatch.setattr(fence, 'admit', refuse)
    _housekeeping_chore('fixture', lambda: pytest.fail('ran during drain'))


@pytest.mark.parametrize('cancel', [False, True])
def test_async_timeout_retains_until_completion_or_revokes_before_cancel_close(custody, monkeypatch, cancel):
    from hermes_cli import worker_drain_capability as capability
    from agent import async_utils
    from gateway.run_drain_work import schedule_chore
    lease, events = custody
    future = Future()
    coroutines = []

    def schedule(coro, loop, **kwargs):
        coroutines.append(coro)
        return future

    monkeypatch.setattr(async_utils, 'safe_schedule_threadsafe', schedule)
    monkeypatch.setattr(capability, 'revoke', lambda reason: events.append('revoke'))

    async def work():
        assert fence._authority.get() is lease

    returned = schedule_chore(work, object())
    assert returned is future
    with pytest.raises(TimeoutError):
        future.result(timeout=0)
    assert not lease.closed
    if cancel:
        future.cancel()
        assert events == ['admit', 'revoke', 'close']
    else:
        future.set_result(None)
        assert events == ['admit', 'close']
    coroutines[0].close()


def test_scheduled_coroutine_enters_lease_authority(custody, monkeypatch):
    import asyncio
    from agent import async_utils
    from gateway.run_drain_work import schedule_chore
    lease, events = custody
    future = Future()
    coroutines = []

    def schedule(coro, loop, **kwargs):
        coroutines.append(coro)
        return future

    monkeypatch.setattr(async_utils, 'safe_schedule_threadsafe', schedule)

    async def work():
        assert fence._authority.get() is lease and not lease.closed
        events.append('work')
        await asyncio.sleep(0)
        assert not lease.closed
        return 'complete'

    schedule_chore(work, object())
    result = asyncio.run(coroutines[0])
    assert result == 'complete' and not lease.closed
    future.set_result(result)
    assert events == ['admit', 'work', 'close']
    assert fence._authority.get() is None


def test_schedule_failure_releases_without_creating_inner_coroutine(custody, monkeypatch):
    from agent import async_utils
    from gateway.run_drain_work import schedule_chore
    lease, events = custody

    def reject(coro, *args, **kwargs):
        coro.close()

    monkeypatch.setattr(async_utils, 'safe_schedule_threadsafe', reject)
    assert schedule_chore(lambda: pytest.fail('must not start'), None) is None
    assert events == ['admit', 'close'] and lease.closed
