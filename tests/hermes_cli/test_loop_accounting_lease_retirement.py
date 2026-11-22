"""Review regression: discarded lease tokens must not own future CPU."""
import threading
import pytest
from hermes_cli import loop_accounting as la
from hermes_cli import host_loop_lease as leases
from types import SimpleNamespace

@pytest.fixture
def cpu_clock(monkeypatch):
    la.reset()
    clock = [1.0]
    tid = la._native_id()
    monkeypatch.setattr(la, '_thread_time', lambda: clock[0])
    monkeypatch.setattr(la.os, 'listdir', lambda _: [])
    monkeypatch.setattr(la.sys, 'platform', 'darwin')  # os-marker: ok — synthetic CPU clock, no native ABI call
    monkeypatch.setattr(la, '_darwin_thread_cpu', lambda: [(tid, 'MainThread', clock[0])])
    monkeypatch.setattr(la.os, 'times', lambda: SimpleNamespace(user=clock[0], system=0.0))
    monkeypatch.setattr(la, '_process_start_time', lambda: 1.0)
    monkeypatch.setattr(la, '_role_guess', lambda: 'test')
    yield clock
    la.reset()

@pytest.mark.parametrize('end', ['release', 'foreign_beat', 'foreign_release'])
def test_discarded_lease_does_not_account_future_unscoped_cpu(cpu_clock, monkeypatch, tmp_path, end):
    monkeypatch.setattr(leases, 'enabled', lambda: False)
    lease = leases.HostLoopLease('review-probe', 'gateway', interval=1, home=tmp_path)
    assert lease.held()
    cpu_clock[0] = 1.04
    if end == 'release':
        lease.release()
    else:
        thread = threading.Thread(target=lease.beat if end == 'foreign_beat' else lease.release)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
    assert lease._pass is None
    start = la.snapshot()
    cpu_clock[0] = 1.14
    finish = la.snapshot()
    before = start['process']['accounted_cpu_s']
    after = finish['process']['accounted_cpu_s']
    assert after - before == pytest.approx(0), (end, start['threads'], finish['threads'])
    assert 'lease.review-probe' not in la.counters()
    lease.release()  # repeated retirement is harmless; a new pass still books normally
    assert lease.held()
    cpu_clock[0] += 0.02
    lease.beat()
    assert la.counters()['lease.review-probe']['runs'] == 1
    assert la.counters()['lease.review-probe']['cpu_s'] == pytest.approx(0.02)


def test_discard_keeps_other_open_intervals_and_closed_inner_cpu(cpu_clock):
    outer = la.span_open('outer')
    abandoned = la.span_open('abandoned')
    with la.pass_('inner'):
        cpu_clock[0] += 0.02
    la.span_discard(abandoned)
    cpu_clock[0] += 0.03
    assert la.snapshot()['process']['accounted_cpu_s'] == pytest.approx(0.05)
    assert la.span_close(outer)
    assert la.snapshot()['process']['accounted_cpu_s'] == pytest.approx(0.05)
    assert 'abandoned' not in la.counters()


def test_discard_is_serialized_with_snapshot(cpu_clock):
    token = la.span_open('discard')
    finished = threading.Event()
    thread = threading.Thread(target=lambda: (la.span_discard(token), finished.set()))
    with la._lock:
        thread.start()
        assert not finished.wait(0.05)
    thread.join(5)
    assert finished.is_set()

