"""A timed-out cron cleanup keeps custody until the real thread unwinds."""
import threading
from types import SimpleNamespace

import pytest

from cron.scheduler import _run_cron_cleanup_with_timeout
from hermes_cli import worker_drain_fence as fence


@pytest.fixture
def custody(monkeypatch):
    events = []
    closed = threading.Event()
    lease = SimpleNamespace(close=lambda: (events.append('close'), closed.set()))
    monkeypatch.setattr(fence, 'admit', lambda purpose: (events.append('admit') or lease))
    return lease, events, closed


def test_timeout_retains_independent_lease_and_thread_authority(custody):
    lease, events, closed = custody
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    observed = []

    def cleanup():
        observed.append(fence._authority.get())
        entered.set()
        try:
            assert release.wait(5)
            events.append('cleanup')
        finally:
            finished.set()

    try:
        assert not _run_cron_cleanup_with_timeout(
            cleanup, job_id='fixture', label='cleanup', timeout_seconds=0.001)
        assert entered.wait(5)
        assert observed == [lease]
        assert events == ['admit'] and not closed.is_set()
        assert fence._authority.get() is None
    finally:
        release.set()
        assert finished.wait(5)
    assert closed.wait(5)
    assert events == ['admit', 'cleanup', 'close']


@pytest.mark.parametrize('timeout', [0, 5])
@pytest.mark.parametrize('failure', [False, True])
def test_cleanup_success_or_failure_releases_after_actual_cleanup(custody, timeout, failure):
    lease, events, closed = custody

    def cleanup():
        assert fence._authority.get() is lease
        assert not closed.is_set()
        events.append('cleanup')
        if failure:
            raise ValueError('fixture')

    assert _run_cron_cleanup_with_timeout(
        cleanup, job_id='fixture', label='cleanup', timeout_seconds=timeout) is not failure
    assert events == ['admit', 'cleanup', 'close']
    assert fence._authority.get() is None


def test_thread_start_failure_releases_custody(custody, monkeypatch):
    from hermes_cli import worker_drain_capability as capability
    _lease, events, closed = custody
    monkeypatch.setattr(capability, 'revoke', lambda reason: events.append('revoke'))

    def reject(self):
        raise RuntimeError('cannot start')

    monkeypatch.setattr(threading.Thread, 'start', reject)
    with pytest.raises(RuntimeError, match='cannot start'):
        _run_cron_cleanup_with_timeout(
            lambda: pytest.fail('must not run'), job_id='fixture', label='cleanup', timeout_seconds=5)
    assert events == ['admit', 'revoke', 'close'] and closed.is_set()


def test_refused_cleanup_never_starts(monkeypatch):
    def refuse(purpose):
        raise fence.DrainRefused('draining')

    monkeypatch.setattr(fence, 'admit', refuse)
    with pytest.raises(fence.DrainRefused):
        _run_cron_cleanup_with_timeout(
            lambda: pytest.fail('unadmitted cleanup'), job_id='fixture', label='cleanup', timeout_seconds=5)


def test_real_timeout_survivor_stays_busy_after_parent_closes(tmp_path, monkeypatch):
    import os

    ident = fence.Identity('i-0123456789abcdef0', fence.read_boot_id(), 'cleanup-proof')
    fence.install_state(tmp_path, ident)
    monkeypatch.setattr(fence, 'load_activation', lambda *a: {'uid': os.getuid()})
    monkeypatch.setattr(fence, '_activation_root', lambda act: tmp_path)
    monkeypatch.setattr(fence, 'current_identity', lambda *a, **k: ident)
    release, entered, exited = threading.Event(), threading.Event(), threading.Event()
    threads = []
    children = []

    def cleanup():
        threads.append(threading.current_thread())
        entered.set()
        try:
            assert release.wait(5)
            children.append(fence.admit('cleanup.child'))
        finally:
            exited.set()

    parent = fence.admit('cron.run')
    try:
        with fence.lease_scope(parent):
            assert not _run_cron_cleanup_with_timeout(
                cleanup, job_id='fixture', label='cleanup', timeout_seconds=0.001)
        assert entered.wait(5)
        parent.close()
        assert fence.busy_leases(tmp_path) == 1
        latch = fence.read_latch(tmp_path, ident)
        latch['phase'] = 'draining'
        fence._write_atomic(tmp_path / 'core-update' / fence.LATCH_FILE, latch)
        with pytest.raises(fence.DrainRefused):
            fence.admit('unrelated')
        release.set()
        assert exited.wait(5)
        threads[0].join(5)
        assert not threads[0].is_alive()
        assert len(children) == 1
        assert fence.busy_leases(tmp_path) == 1  # child retains its own lease
    finally:
        release.set()
        parent.close()
        for thread in threads:
            thread.join(5)
        for child in children:
            child.close()
    assert fence.busy_leases(tmp_path) == 0
