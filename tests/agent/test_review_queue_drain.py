"""Deferred snapshots own independent drain custody, not a copied turn fd."""
from types import SimpleNamespace

import pytest

from agent import review_idle_queue as queue
from hermes_cli import worker_drain_fence as fence


@pytest.fixture
def harness(monkeypatch):
    events, leases = [], []

    class Lease:
        closed = False

        def close(self):
            assert not self.closed
            self.closed = True
            events.append(('close', self))

    def admit(purpose):
        lease = Lease()
        leases.append(lease)
        events.append(('admit', lease))
        return lease

    monkeypatch.setattr(fence, 'admit', admit)
    q = queue.ReviewIdleQueue()
    monkeypatch.setattr(q, '_ensure_thread', lambda: None)
    q._update_held = lambda: False
    q._now = lambda: 10000
    q._server_idle = lambda: True
    q._quiet_since = 0
    return q, events, leases


def test_queue_retains_lease_and_replacement_has_no_gap(harness):
    q, events, leases = harness
    first, second = lambda: None, lambda: None
    q.enqueue(None, 's', {}, dispatch=first)
    assert len(leases) == 1 and not leases[0].closed
    q.enqueue(None, 's', {}, dispatch=second)
    assert [e[0] for e in events] == ['admit', 'admit', 'close']
    q.discard('s', first)
    assert not leases[1].closed
    q.discard('s', second)
    assert leases[1].closed and q.pending_count() == 0


def test_dispatch_uses_retained_authority_and_releases_after_failure(harness):
    q, events, leases = harness

    def dispatch():
        assert fence._authority.get() is leases[0]
        assert not leases[0].closed
        events.append(('dispatch', None))
        raise ValueError('dispatch failure')

    q.enqueue(None, 's', {}, dispatch=dispatch)
    item = q._pop_dispatchable()
    assert item is not None and not leases[0].closed
    try:
        with pytest.raises(ValueError, match='dispatch failure'):
            item.context.run(q._dispatch, item)
        assert leases[0].closed
        assert [e[0] for e in events] == ['admit', 'dispatch', 'close']
    finally:
        if item.work is not None:
            item.work.release()


def test_spawn_failure_removes_and_releases_queued_custody(harness, monkeypatch):
    q, _, leases = harness

    def fail():
        raise RuntimeError('thread start failed')

    monkeypatch.setattr(q, '_ensure_thread', fail)
    with pytest.raises(RuntimeError, match='thread start failed'):
        q.enqueue(None, 's', {})
    assert q.pending_count() == 0
    assert leases[0].closed


def test_reservation_failure_preserves_queue_and_lease(harness, monkeypatch):
    from agent import local_work_agent
    q, _, leases = harness
    q.enqueue(None, 's', {})

    def fail(*args):
        raise RuntimeError('reservation failed')

    monkeypatch.setattr(local_work_agent, 'reserve', fail)
    with pytest.raises(RuntimeError, match='reservation failed'):
        q._pop_dispatchable()
    assert q.pending_count() == 1
    assert not leases[0].closed
    q.discard('s', None)
    assert leases[0].closed


def test_disabled_review_releases_without_spawn(harness, monkeypatch):
    q, _, leases = harness
    agent = SimpleNamespace(_spawn_background_review_now=lambda **kw: pytest.fail('disabled spawn'))
    q.enqueue(agent, 's', {})
    monkeypatch.setattr(q, '_still_enabled', lambda item: False)
    q._dispatch(q._pending.pop('s'))
    assert leases[0].closed


def test_dispatch_cleanup_keeps_lease_even_when_reservation_cleanup_raises(harness):
    q, events, leases = harness
    q.enqueue(None, 's', {}, dispatch=lambda: None)
    item = q._pending.pop('s')

    def release():
        assert not leases[0].closed
        events.append(('cleanup', None))
        raise ValueError('cleanup failure')

    item.work = SimpleNamespace(release=release)
    with pytest.raises(ValueError, match='cleanup failure'):
        q._dispatch(item)
    assert leases[0].closed
    assert [e[0] for e in events] == ['admit', 'cleanup', 'close']
    item.finish()  # outer dispatcher cleanup is idempotent


def test_real_queued_lease_survives_turn_close_and_admits_child_during_drain(tmp_path, monkeypatch):
    import os

    ident = fence.Identity('i-0123456789abcdef0', fence.read_boot_id(), 'queue-proof')
    fence.install_state(tmp_path, ident)
    monkeypatch.setattr(fence, 'load_activation', lambda *a: {'uid': os.getuid()})
    monkeypatch.setattr(fence, '_activation_root', lambda act: tmp_path)
    monkeypatch.setattr(fence, 'current_identity', lambda *a, **k: ident)
    q = queue.ReviewIdleQueue()
    monkeypatch.setattr(q, '_ensure_thread', lambda: None)
    children = []

    def dispatch():
        children.append(fence.admit('retained.child'))

    parent = fence.admit('turn')
    with fence.lease_scope(parent):
        q.enqueue(None, 's', {}, dispatch=dispatch)
    parent.close()
    assert fence.busy_leases(tmp_path) == 1
    latch = fence.read_latch(tmp_path, ident)
    latch['phase'] = 'draining'
    fence._write_atomic(tmp_path / 'core-update' / fence.LATCH_FILE, latch)
    item = q._pending.pop('s')
    try:
        item.context.run(q._dispatch, item)
        assert len(children) == 1
        assert fence.busy_leases(tmp_path) == 1  # child survives queue release
        with pytest.raises(fence.DrainRefused):
            fence.admit('unrelated')
    finally:
        item.close_drain()
        for child in children:
            child.close()
    assert fence.busy_leases(tmp_path) == 0
