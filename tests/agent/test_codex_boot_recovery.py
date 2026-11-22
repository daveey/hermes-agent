"""Managed construction must never wait for exhausted-account recovery HTTP."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from agent import codex_boot_recovery as boot
from agent.credential_pool import load_pool
from agent.managed_worker import _construct_agent
from hermes_cli import auth_codex, auth_codex_quota_negative as negative
from tests.hermes_cli.test_auth_codex_quota_probe import (
    _jwt, _pool_only_rate_limited_store, _write_auth_store,
)


@pytest.fixture
def setup_pool(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setenv('HERMES_HOME', str(home))
    row = _pool_only_rate_limited_store()
    exhausted = row['credential_pool']['openai-codex'][0]
    exhausted['access_token'] = _jwt({'exp': time.time() + 3600})
    _write_auth_store(home, row)
    monkeypatch.setattr('gateway.session_worker_construct.worker_route', lambda *a: {})
    monkeypatch.setattr('gateway.session_kanban.record_start_failure', lambda *a: None)
    auth_codex._codex_quota_probe_cache.clear()
    auth_codex._codex_quota_probe_evidence.clear()
    return home, row


def construct(factory, kanban=True):
    policy = SimpleNamespace()
    frame = {'policy': {'kanban_json': json.dumps({'task_id': 'fixture'}) if kanban else 'null'}}
    return _construct_agent(frame, factory, policy)


@pytest.mark.parametrize('memo', ['cold', 'missing', 'stale', 'invalid'])
@pytest.mark.parametrize('usable', [False, True])
def test_managed_boot_never_enters_recovery_http(setup_pool, monkeypatch, memo, usable):
    home, store = setup_pool
    if usable:
        row = dict(store['credential_pool']['openai-codex'][0])
        row.update(id='usable', priority=1, last_status=None, last_error_code=None,
                   last_error_reason=None, last_error_message=None, last_error_reset_at=None)
        store['credential_pool']['openai-codex'].append(row)
        _write_auth_store(home, store)
    row = store['credential_pool']['openai-codex'][0]
    key = negative.key(row['access_token'], auth_codex._codex_usage_probe_url(None),
                       row['last_error_reset_at'], row['last_status_at'])
    if memo != 'cold':
        path = negative._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text({'missing': '{}', 'stale': json.dumps({key: time.time() - 600}),
                         'invalid': '{broken'}[memo])
    assert not negative.hit(key, 300)
    starts, calls = [], []
    monkeypatch.setattr(boot, '_start', lambda ctx: starts.append(ctx))
    def forbidden(**kwargs):
        calls.append(memo)
        raise AssertionError('boot entered recovery HTTP')
    monkeypatch.setattr(auth_codex, '_codex_http_client', forbidden)
    chosen = construct(lambda **kw: load_pool('openai-codex').select())
    assert calls == []
    assert chosen.id == 'usable' if usable else chosen is None
    assert len(starts) == 1
    assert boot._pending.get() is None


def test_expired_recovery_refresh_also_deferred(setup_pool, monkeypatch):
    home, store = setup_pool
    entry = store['credential_pool']['openai-codex'][0]
    entry.update(access_token=_jwt({'exp': time.time() - 3600}), refresh_token='fixture-refresh')
    _write_auth_store(home, store)
    calls = []
    monkeypatch.setattr(boot, '_start', lambda ctx: None)
    def refresh(*a):
        calls.append(True)
        raise AssertionError('boot refreshed exhausted token')
    monkeypatch.setattr(auth_codex, 'refresh_codex_oauth_pure', refresh)
    assert construct(lambda **kw: load_pool('openai-codex').select()) is None
    assert calls == []


def test_failed_boot_does_not_run_synchronous_completion_recovery(setup_pool, monkeypatch):
    monkeypatch.setattr(boot, '_start', lambda ctx: None)
    calls = []
    def recovery(exc):
        calls.append(exc)
        return False
    monkeypatch.setattr('hermes_cli.kanban_quota_recovery.recover_exception', recovery)
    def factory(**kw):
        assert load_pool('openai-codex').select() is None
        raise RuntimeError('quota exhausted')
    with pytest.raises(RuntimeError, match='quota exhausted'):
        construct(factory)
    assert calls == []


def test_background_recovery_is_off_boot_and_eventually_persisted(setup_pool, monkeypatch):
    from tests.hermes_cli.test_auth_codex_quota_probe import _StubClient, _StubResponse
    queued, calls = [], []
    monkeypatch.setattr(boot, '_start', lambda ctx: queued.append(ctx))
    monkeypatch.setattr(auth_codex, '_codex_http_client',
                        lambda **kw: _StubClient(calls, _StubResponse(200, {'rate_limit': {'primary_window': {
                            'used_percent': 0, 'reset_at': time.time() + 86400 * 30}}})))
    assert construct(lambda **kw: load_pool('openai-codex').select()) is None
    assert calls == []
    queued.pop().run(boot._recover)
    assert len(calls) == 1
    chosen = construct(lambda **kw: load_pool('openai-codex').select())
    assert chosen is not None
    assert chosen.last_status == 'ok'


def test_non_kanban_retains_inline_recovery(setup_pool, monkeypatch):
    from tests.hermes_cli.test_auth_codex_quota_probe import _StubClient, _StubResponse
    calls = []
    monkeypatch.setattr(auth_codex, '_codex_http_client',
                        lambda **kw: _StubClient(calls, _StubResponse(200, {'rate_limit': {'primary_window': {
                            'used_percent': 0, 'reset_at': time.time() + 86400 * 30}}})))
    chosen = construct(lambda **kw: load_pool('openai-codex').select(), kanban=False)
    assert chosen is not None
    assert len(calls) == 1


def test_background_start_is_nonblocking_and_deduplicated(setup_pool, monkeypatch):
    from contextvars import copy_context
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def recover():
        calls.append(True)
        assert boot._pending.get() is None
        entered.set()
        release.wait(5)
        finished.set()
    monkeypatch.setattr(boot, '_recover', recover)
    thread = None
    try:
        with boot.construction_scope(True):
            assert boot.defer_recovery()
            assert not entered.is_set()
        assert entered.wait(2)
        thread = next(t for t in threading.enumerate() if t.name == 'codex-boot-recovery')
        boot._start(copy_context())
        assert len(calls) == 1
    finally:
        release.set()
        assert finished.wait(2)
        if thread is not None:
            thread.join(2)
            assert not thread.is_alive()
