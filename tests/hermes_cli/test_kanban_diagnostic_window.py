"""Diagnostic pulls wait for a bounded quiet window, never bypass a busy pull."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_sync_diagnostic as diagnostic


def test_busy_and_more_retry_then_return_fresh(monkeypatch):
    results = [client.Pulled(busy=True), client.Pulled(more=True), client.Pulled()]
    calls = []
    clock = [0.0]
    monkeypatch.setattr(diagnostic.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(diagnostic, '_wait', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    def pull(*args, **kw):
        calls.append(kw)
        return results.pop(0)
    monkeypatch.setattr(client, '_pull_replica', pull)
    assert not diagnostic.refresh('default', SimpleNamespace(), 'home', None).busy
    assert calls == [{'diagnostic': True}] * 3


def test_attempt_cap_preserves_busy(monkeypatch):
    calls = []
    monkeypatch.setattr(diagnostic, '_wait', lambda seconds: None)
    monkeypatch.setattr(client, '_pull_replica', lambda *a, **kw: calls.append(kw) or client.Pulled(busy=True))
    result = diagnostic.refresh('default', None, 'home', None)
    assert result.busy
    assert len(calls) == diagnostic.MAX_ATTEMPTS


def test_time_cap_does_not_start_another_pull(monkeypatch):
    clock = [0.0]
    calls = []
    monkeypatch.setattr(diagnostic.time, 'monotonic', lambda: clock[0])
    def pull(*a, **kw):
        calls.append(kw)
        clock[0] += diagnostic.MAX_SECONDS
        return client.Pulled(more=True)
    monkeypatch.setattr(client, '_pull_replica', pull)
    assert diagnostic.refresh('default', None, 'home', None).more
    assert len(calls) == 1


def test_errors_are_not_busy_retries(monkeypatch):
    def pull(*a, **kw):
        raise OSError('offline')
    monkeypatch.setattr(client, '_pull_replica', pull)
    with pytest.raises(OSError, match='offline'):
        diagnostic.refresh('default', None, 'home', None)
