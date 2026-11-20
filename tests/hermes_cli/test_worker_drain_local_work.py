"""Independent task/inline custody, including updater-disabled gateways."""
import asyncio
import os
from types import SimpleNamespace

import pytest

from hermes_cli import local_work_threads as work
from hermes_cli import worker_drain_fence as fence
from hermes_cli.local_work import Registry


@pytest.fixture
def custody(monkeypatch):
    events = []
    lease = SimpleNamespace(close=lambda: events.append('close'))
    monkeypatch.setattr(fence, 'admit', lambda purpose: (events.append('admit') or lease))
    monkeypatch.setattr(work, 'installed_registry', lambda: None)
    return lease, events


def test_inline_without_updater_has_own_authority(custody):
    lease, events = custody
    with work.work_scope('inbound', 'fixture') as handle:
        assert handle is not None
        assert fence._authority.get() is lease
        assert events == ['admit']
    assert fence._authority.get() is None
    assert events == ['admit', 'close']
    work.end_work(handle)
    assert events == ['admit', 'close']


def test_inline_exception_releases(custody):
    _lease, events = custody
    with pytest.raises(ValueError):
        with work.work_scope('inbound', 'fixture'):
            raise ValueError('handler')
    assert events == ['admit', 'close']


def test_registry_reservation_failure_releases_drain(custody, monkeypatch, tmp_path):
    _lease, events = custody
    registry = Registry(homes=[str(tmp_path)])
    monkeypatch.setattr(work, 'installed_registry', lambda: registry)

    def fail(*a, **k):
        events.append('reserve')
        raise ValueError('reserve')

    monkeypatch.setattr(registry, 'reserve', fail)
    with pytest.raises(ValueError, match='reserve'):
        work.begin_work('inbound', 'fixture', home=str(tmp_path))
    assert events == ['admit', 'reserve', 'close']


def test_refusal_precedes_task_creation(monkeypatch):
    monkeypatch.setattr(work, 'installed_registry', lambda: None)

    def refuse(purpose):
        raise fence.DrainRefused('draining')

    monkeypatch.setattr(fence, 'admit', refuse)
    with pytest.raises(fence.DrainRefused):
        work.create_tracked_task(lambda: pytest.fail('not admitted'), 'inbound', 'fixture')


def test_task_retains_through_cancellation_cleanup(custody):
    lease, events = custody

    async def scenario():
        entered, cleanup = asyncio.Event(), asyncio.Event()

        async def target():
            assert fence._authority.get() is lease
            entered.set()
            try:
                await asyncio.Future()
            finally:
                assert fence._authority.get() is lease
                assert events == ['admit']
                await cleanup.wait()

        task = work.create_tracked_task(lambda: asyncio.create_task(target()), 'inbound', 'fixture')
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and events == ['admit']
        cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert events == ['admit', 'close']
        assert fence._authority.get() is None

    asyncio.run(scenario())


def test_cancel_before_start_releases(custody):
    _lease, events = custody

    async def scenario():
        async def target():
            pytest.fail('cancelled before entry')

        task = work.create_tracked_task(lambda: asyncio.create_task(target()), 'inbound', 'fixture')
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert events == ['admit', 'close']

    asyncio.run(scenario())


def test_uncertain_create_revokes_before_release(custody, monkeypatch):
    from hermes_cli import worker_drain_capability as capability
    _lease, events = custody
    monkeypatch.setattr(capability, 'revoke', lambda reason: events.append('revoke'))

    def fail():
        events.append('create')
        raise RuntimeError('may have scheduled')

    with pytest.raises(RuntimeError, match='may have scheduled'):
        work.create_tracked_task(fail, 'inbound', 'fixture')
    assert events == ['admit', 'create', 'revoke', 'close']


def test_updater_and_drain_bind_independently(custody, monkeypatch, tmp_path):
    from hermes_cli.local_work import ACTIVE_SCOPE
    lease, events = custody
    registry = Registry(homes=[str(tmp_path)])
    monkeypatch.setattr(work, 'installed_registry', lambda: registry)
    with work.work_scope('inbound', 'fixture', home=str(tmp_path)):
        assert registry.snapshot().active == 1
        assert ACTIVE_SCOPE.get().home == str(tmp_path)
        assert fence._authority.get() is lease
    assert registry.snapshot().active == 0
    assert ACTIVE_SCOPE.get() is None
    assert events == ['admit', 'close']


def test_untrackable_future_revokes_before_release(custody, monkeypatch):
    from concurrent.futures import Future
    from hermes_cli import worker_drain_capability as capability
    _lease, events = custody
    monkeypatch.setattr(capability, 'revoke', lambda reason: events.append('revoke'))
    future = Future()  # cancellation does NOT prove physical executor cleanup
    assert work.create_tracked_task(lambda: future, 'inbound', 'fixture') is future
    assert events == ['admit', 'revoke', 'close']


def test_callback_registration_failure_revokes(custody, monkeypatch):
    from hermes_cli import worker_drain_capability as capability
    _lease, events = custody
    monkeypatch.setattr(capability, 'revoke', lambda reason: events.append('revoke'))

    class BadFuture(asyncio.Future):
        def add_done_callback(self, *a, **k):
            raise RuntimeError('no receipt')

    async def scenario():
        future = BadFuture()
        with pytest.raises(RuntimeError, match='no receipt'):
            work.create_tracked_task(lambda: future, 'inbound', 'fixture')
        future.cancel()
        assert events == ['admit', 'revoke', 'close']

    asyncio.run(scenario())


def test_inactive_without_updater_retains_noop(monkeypatch):
    monkeypatch.setattr(work, 'installed_registry', lambda: None)
    monkeypatch.setattr(fence, 'admit', lambda purpose: None)
    assert work.begin_work('inbound', 'fixture') is None
    with work.work_scope('inbound', 'fixture') as handle:
        assert handle is None
    sentinel = object()
    assert work.create_tracked_task(lambda: sentinel, 'inbound', 'fixture') is sentinel


def test_real_task_child_after_parent_close_and_drain(tmp_path, monkeypatch):
    ident = fence.Identity('i-0123456789abcdef0', fence.read_boot_id(), 'task-proof')
    fence.install_state(tmp_path, ident)
    monkeypatch.setattr(fence, 'load_activation', lambda *a: {'uid': os.getuid()})
    monkeypatch.setattr(fence, '_activation_root', lambda act: tmp_path)
    monkeypatch.setattr(fence, 'current_identity', lambda *a, **k: ident)
    monkeypatch.setattr(work, 'installed_registry', lambda: None)

    async def scenario():
        release = asyncio.Event()
        children = []

        async def target():
            await release.wait()
            children.append(fence.admit('task.child'))

        parent = fence.admit('parent')
        task = None
        try:
            with fence.lease_scope(parent):
                task = work.create_tracked_task(lambda: asyncio.create_task(target()), 'inbound', 'fixture')
            parent.close()
            assert fence.busy_leases(tmp_path) == 1
            latch = fence.read_latch(tmp_path, ident)
            latch['phase'] = 'draining'
            fence._write_atomic(tmp_path / 'core-update' / fence.LATCH_FILE, latch)
            with pytest.raises(fence.DrainRefused):
                fence.admit('unrelated')
            release.set()
            await task
            await asyncio.sleep(0)
            assert len(children) == 1 and fence.busy_leases(tmp_path) == 1
        finally:
            release.set()
            if task is not None:
                await task
            parent.close()
            for child in children:
                child.close()
        assert fence.busy_leases(tmp_path) == 0

    asyncio.run(scenario())
