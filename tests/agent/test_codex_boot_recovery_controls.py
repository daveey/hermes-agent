"""Fail-closed and lifecycle controls for the off-boot Codex probe."""
from contextvars import copy_context
import threading
import time

import pytest

from agent import codex_boot_recovery as boot
from agent.credential_pool import load_pool
from hermes_cli import auth_codex
from tests.agent.test_codex_boot_recovery import construct, setup_pool as _setup_pool
from tests.hermes_cli.test_auth_codex_quota_probe import _StubClient, _StubResponse, _write_auth_store


setup_pool = _setup_pool


@pytest.mark.parametrize('status,payload', [
    (429, {}), (500, {}), (200, {}),
    (200, {'rate_limit': {'primary_window': {'used_percent': 100, 'reset_at': 9999999999}}}),
])
def test_unknown_or_exhausted_background_evidence_never_selects(setup_pool, monkeypatch, status, payload):
    queued, calls = [], []
    monkeypatch.setattr(boot, '_start', queued.append)
    monkeypatch.setattr(auth_codex, '_codex_http_client',
                        lambda **kw: _StubClient(calls, _StubResponse(status, payload)))
    assert construct(lambda **kw: load_pool('openai-codex').select()) is None
    queued.pop().run(boot._recover)
    assert len(calls) == 1
    assert construct(lambda **kw: load_pool('openai-codex').select()) is None
    assert len(calls) == 1


def test_new_failure_during_background_probe_wins(setup_pool, monkeypatch):
    home, store = setup_pool
    queued, calls = [], []
    monkeypatch.setattr(boot, '_start', queued.append)

    class RacingClient(_StubClient):
        def get(self, url, headers=None):
            row = store['credential_pool']['openai-codex'][0]
            row['last_status_at'] = time.time() + 100
            _write_auth_store(home, store)
            return super().get(url, headers)

    monkeypatch.setattr(auth_codex, '_codex_http_client', lambda **kw: RacingClient(
        calls, _StubResponse(200, {'rate_limit': {'primary_window': {
            'used_percent': 0, 'reset_at': 9999999999}}})))
    assert construct(lambda **kw: load_pool('openai-codex').select()) is None
    queued.pop().run(boot._recover)
    assert len(calls) == 1
    assert construct(lambda **kw: load_pool('openai-codex').select()) is None


def test_usable_only_construction_schedules_nothing(setup_pool, monkeypatch):
    home, store = setup_pool
    store['credential_pool']['openai-codex'][0].update(
        last_status='ok', last_error_reset_at=None)
    _write_auth_store(home, store)
    queued = []
    monkeypatch.setattr(boot, '_start', queued.append)
    assert construct(lambda **kw: load_pool('openai-codex').select()) is not None
    assert queued == []


def test_nested_scope_queues_once_and_restores_context(monkeypatch):
    queued = []
    monkeypatch.setattr(boot, '_start', queued.append)
    with pytest.raises(ValueError):
        with boot.construction_scope(True):
            with boot.construction_scope(True):
                assert boot.defer_recovery()
            assert queued == []
            raise ValueError('fixture')
    assert len(queued) == 1
    assert boot._pending.get() is None
    assert queued[0].run(boot.defer_recovery) is False


def test_thread_start_failure_is_fail_closed_and_releases_slot(setup_pool, monkeypatch):
    class BrokenThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError('fixture start failure')

    monkeypatch.setattr(threading, 'Thread', BrokenThread)
    boot._start(copy_context())
    assert not boot._running
    assert construct(lambda **kw: load_pool('openai-codex').select()) is None
    assert not boot._running
