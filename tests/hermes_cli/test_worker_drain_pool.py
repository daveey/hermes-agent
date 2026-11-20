"""Physical executor custody survives cancellation of the async observer."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import os
import threading

import pytest

from hermes_cli import local_work_threads as work
from hermes_cli import worker_drain_fence as fence


@pytest.fixture
def activated(tmp_path, monkeypatch):
    ident = fence.Identity('i-0123456789abcdef0', fence.read_boot_id(), 'pool-proof')
    fence.install_state(tmp_path, ident)
    monkeypatch.setattr(fence, 'load_activation', lambda *a: {'uid': os.getuid()})
    monkeypatch.setattr(fence, '_activation_root', lambda act: tmp_path)
    monkeypatch.setattr(fence, 'current_identity', lambda *a, **k: ident)
    monkeypatch.setattr(work, 'installed_registry', lambda: None)
    return tmp_path, ident


def test_cancelled_pool_observer_retains_target_authority(activated):
    root, ident = activated
    entered, release = threading.Event(), threading.Event()
    children = []

    def target():
        entered.set()
        assert release.wait(5)
        children.append(fence.admit('pool.child'))

    async def scenario(pool):
        task = asyncio.create_task(work.run_in_pool(pool, copy_context(), target))
        try:
            for _ in range(1000):
                if entered.is_set():
                    break
                await asyncio.sleep(0.001)
            assert entered.is_set()
            assert fence.busy_leases(root) == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert fence.busy_leases(root) == 1
            latch = fence.read_latch(root, ident)
            latch['phase'] = 'draining'
            fence._write_atomic(root / 'core-update' / fence.LATCH_FILE, latch)
            with pytest.raises(fence.DrainRefused):
                fence.admit('unrelated')
        finally:
            release.set()
            if not task.done():
                await task

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            asyncio.run(scenario(pool))
        assert len(children) == 1
        assert fence.busy_leases(root) == 1
    finally:
        release.set()
        for child in children:
            child.close()
    assert fence.busy_leases(root) == 0


def test_queued_cancel_releases_without_running(activated):
    root, _ident = activated
    entered, release = threading.Event(), threading.Event()

    def blocker():
        entered.set()
        assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(blocker)  # fixture infrastructure, not accepted gateway work
        try:
            assert entered.wait(5)
            queued = work.submit_tracked(pool, lambda: pytest.fail('cancelled target'), source='fixture')
            assert fence.busy_leases(root) == 1
            assert queued.cancel()
            assert fence.busy_leases(root) == 0
        finally:
            release.set()
            running.result(5)


def test_shutdown_pool_refusal_releases(activated):
    root, _ident = activated
    pool = ThreadPoolExecutor(max_workers=1)
    pool.shutdown()
    with pytest.raises(RuntimeError, match='shutdown'):
        work.submit_tracked(pool, lambda: None, source='fixture')
    assert fence.busy_leases(root) == 0


def test_enqueue_then_raise_retains_until_actual_target(activated):
    root, _ident = activated
    calls = []

    class UncertainExecutor:
        def submit(self, call):
            calls.append(call)
            raise RuntimeError('queued before failure')

    with pytest.raises(RuntimeError, match='queued before failure'):
        work.submit_tracked(UncertainExecutor(), lambda: 'done', source='fixture')
    assert fence.busy_leases(root) == 1
    assert calls[0]() == 'done'
    assert fence.busy_leases(root) == 0


def test_thread_target_keeps_independent_lease(activated):
    root, _ident = activated
    entered, release = threading.Event(), threading.Event()
    observed = []

    def target():
        observed.append(fence._authority.get())
        entered.set()
        assert release.wait(5)

    thread = work.TrackedThread(target=target)
    assert fence.busy_leases(root) == 0  # construction is not work admission
    try:
        thread.start()
        assert entered.wait(5)
        assert observed[0] is not None
        assert fence.busy_leases(root) == 1
        thread.join(0)
        assert fence.busy_leases(root) == 1
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert fence.busy_leases(root) == 0


def test_default_pool_without_updater_has_custody(activated):
    root, _ident = activated

    def target():
        assert fence._authority.get() is not None
        assert fence.busy_leases(root) >= 1
        return 'done'

    async def scenario():
        return await work.run_in_pool(None, copy_context(), target)

    assert asyncio.run(scenario()) == 'done'
    assert fence.busy_leases(root) == 0
