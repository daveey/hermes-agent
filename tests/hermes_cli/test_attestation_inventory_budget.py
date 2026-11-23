"""Advisory inventory expiry releases the audit lock without approving partial work."""
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import code_attestation as ca


def ledger():
    result = object.__new__(ca.Attestation)
    result._lock = threading.RLock()
    result._modules = {}
    result._generation = 0
    result._unknown = set()
    result.core_root = '/fixture'
    result.boot_sha = 'a' * 40
    result.route = 'managed-stub'
    return result


def test_inventory_expiry_stops_scan_releases_lock_and_never_returns_positive(monkeypatch):
    a = ledger()
    clock = [10.0]
    monkeypatch.setattr(ca.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(a, '_module_snapshot', lambda: [('first', SimpleNamespace(__file__='one.py')),
                                                       ('second', SimpleNamespace(__file__='two.py'))])
    seen = []
    def inspect(name, filename):
        seen.append((name, filename))
        clock[0] += 1
    monkeypatch.setattr(a, '_module_file', inspect)
    with pytest.raises(TimeoutError):
        a.integrity_observation(deadline=10.5)
    assert seen == [('first', 'one.py')]
    acquired = []
    def other_thread():
        ok = a._lock.acquire(blocking=False)
        acquired.append(ok)
        if ok:
            a._lock.release()
    t = threading.Thread(target=other_thread)
    t.start()
    t.join(5)
    assert acquired == [True]
    # Actual admission remains a complete fresh scan, including mutable paths.
    observed = a.integrity_observation()
    assert observed['boot_sha'] == 'a' * 40
    assert seen == [('first', 'one.py'), ('first', 'one.py'), ('second', 'two.py')]


def test_expired_budget_does_not_wait_on_busy_audit_lock(monkeypatch):
    a = ledger()
    monkeypatch.setattr(ca.time, 'monotonic', lambda: 11.0)
    class NoAcquire:
        def acquire(self, **kwargs):
            pytest.fail('expired observation tried acquiring the global audit lock')
    a._lock = NoAcquire()
    with pytest.raises(TimeoutError):
        a.integrity_observation(deadline=10.0)


def test_inventory_expiry_after_final_item_refuses_partial_positive(monkeypatch):
    a = ledger()
    clock = [10.0]
    monkeypatch.setattr(ca.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(a, '_module_snapshot', lambda: [('only', SimpleNamespace(__file__='one.py'))])
    monkeypatch.setattr(a, '_module_file', lambda *args: clock.__setitem__(0, 12.0))
    with pytest.raises(TimeoutError):
        a.integrity_observation(deadline=11.0)


def test_public_observation_forwards_budget(monkeypatch):
    a = ledger()
    monkeypatch.setattr(ca, '_installed', a)
    seen = []
    monkeypatch.setattr(a, 'integrity_observation', lambda **kwargs: seen.append(kwargs))
    ca.integrity_observation(deadline=12.0)
    assert seen == [{'deadline': 12.0}]


def test_lock_acquisition_timeout_refuses_without_scanning(monkeypatch):
    a = ledger()
    monkeypatch.setattr(ca.time, 'monotonic', lambda: 10.0)
    calls = []
    class BusyLock:
        def acquire(self, **kwargs):
            calls.append(kwargs)
            return False
        def release(self):
            pytest.fail('released an unheld audit lock')
    a._lock = BusyLock()
    monkeypatch.setattr(a, '_inventory_modules', lambda **kwargs: pytest.fail('scanned without lock'))
    with pytest.raises(TimeoutError):
        a.integrity_observation(deadline=10.25)
    assert calls == [{'timeout': 0.25}]


def test_plugin_verification_expiry_never_returns_positive(monkeypatch):
    from hermes_cli import plugin_review_records
    a = ledger()
    a._reviewed_plugins = {'fixture': object()}
    clock = [10.0]
    monkeypatch.setattr(ca.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(a, '_module_snapshot', list)
    verified = []
    def verify(current):
        assert current is a
        verified.append(True)
        clock[0] = 12.0
    monkeypatch.setattr(plugin_review_records, 'verify_registered', verify)
    with pytest.raises(TimeoutError):
        a.integrity_observation(deadline=11.0)
    assert verified == [True]
    assert a.integrity_observation()['boot_sha'] == 'a' * 40
    assert verified == [True, True]
