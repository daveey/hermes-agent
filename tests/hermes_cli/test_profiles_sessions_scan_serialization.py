"""The uncached aggregate list must not multiply SQLite work during poll bursts."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.web_routers import profiles


def test_route_serializes_scans_without_caching_or_losing_query_params(monkeypatch):
    app = FastAPI()
    app.include_router(profiles.sessions_router)
    active = 0
    maximum = 0
    calls = []
    guard = threading.Lock()
    overlap = threading.Event()
    first = threading.Event()
    release = threading.Event()

    def targets(label, home, errors, read):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
            index = len(calls)
            calls.append(label)
            if active > 1:
                overlap.set()
        if index == 0:
            first.set()
            assert release.wait(10)
        with guard:
            active -= 1
        return []

    monkeypatch.setattr(profiles, '_profile_targets', lambda label: [('default', None)])
    monkeypatch.setattr(profiles, '_read_profile_db', targets)
    monkeypatch.setattr(profiles, '_corrupt_profile_stores', lambda targets: {})
    for name in ('kanban', 'goal', 'cron', 'background', 'turn'):
        monkeypatch.setattr(profiles, f'annotate_{name}_activity', lambda rows, **kwargs: None)
    monkeypatch.setattr(profiles, 'annotate_review_wait', lambda rows: None)
    ready = threading.Barrier(5)

    def request(limit):
        ready.wait(timeout=10)
        with TestClient(app) as client:
            response = client.get('/api/profiles/sessions', params={'limit': limit})
            assert response.status_code == 200
            return response.json()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(request, limit) for limit in (20, 20, 50, 100)]
        ready.wait(timeout=10)
        try:
            assert first.wait(10)
            # Baseline deterministically enters another scan while the first is held.
            # This backstop is not the invariant: maximum and every scan result are.
            overlap.wait(2)
        finally:
            release.set()
        results = [future.result(timeout=10) for future in futures]
    assert maximum == 1
    assert len(calls) == 4  # identical polls aren't served stale cached results
    assert [r['limit'] for r in results] == [20, 20, 50, 100]
    with TestClient(app) as client:
        assert client.get('/api/profiles/sessions?limit=20').status_code == 200
    assert len(calls) == 5


def test_scan_exception_releases_admission(monkeypatch):
    def fail(name, home, errors, read):
        raise RuntimeError('failed scan')
    monkeypatch.setattr(profiles, '_profile_targets', lambda label: [('default', None)])
    monkeypatch.setattr(profiles, '_read_profile_db', fail)
    for _ in range(2):
        with pytest.raises(RuntimeError, match='failed scan'):
            profiles.get_profiles_sessions(limit=20, offset=0)
