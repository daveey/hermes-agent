"""Fresh worker processes must reuse only scoped negative quota evidence."""
import base64
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from hermes_cli import auth, auth_codex


def token(name='one'):
    claim = base64.urlsafe_b64encode(json.dumps({'sub': name, 'exp': 9999999999}).encode()).decode().rstrip('=')
    return f'e30.{claim}.sig'


def test_negative_probe_survives_fresh_process_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    calls = []

    @contextmanager
    def client(**kwargs):
        calls.append(kwargs)
        yield SimpleNamespace(get=lambda *a, **k: SimpleNamespace(status_code=429))

    monkeypatch.setattr(auth_codex, '_codex_http_client', client)
    auth._codex_quota_probe_cache.clear()
    scope = dict(base_url='https://example.test/backend-api/codex', failure_at=100., reset_at=10000.)
    assert auth_codex._probe_codex_quota_restored(token(), **scope) is False
    auth._codex_quota_probe_cache.clear()  # no process-local throttle, as at a fresh worker boot
    assert auth_codex._probe_codex_quota_restored(token(), **scope) is False
    assert len(calls) == 1


def test_negative_memo_is_shared_by_real_subprocesses(tmp_path):
    import os
    import subprocess
    import sys
    script = tmp_path / 'selector.py'
    script.write_text('''from contextlib import contextmanager
from pathlib import Path
from hermes_cli import auth_codex
@contextmanager
def client(**kw):
    with Path(__file__).with_suffix('.calls').open('a') as out: out.write('call\\n')
    class Response: status_code = 429
    class Client:
        def get(self, *a, **kw): return Response()
    yield Client()
auth_codex._codex_http_client = client
assert auth_codex._probe_codex_quota_restored(TOKEN, failure_at=100, reset_at=1000) is False
'''.replace('TOKEN', repr(token())))
    from pathlib import Path
    env = dict(os.environ, HERMES_HOME=str(tmp_path / 'home'),
               PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    for _ in range(2):
        subprocess.run([sys.executable, str(script)], env=env, check=True, timeout=20)
    assert script.with_suffix('.calls').read_text().splitlines() == ['call']


@pytest.mark.parametrize('change', ['token', 'url', 'reset', 'failure', 'home', 'ttl', 'future', 'corrupt'])
def test_negative_memo_never_crosses_evidence_scope(tmp_path, monkeypatch, change):
    from hermes_cli import auth_codex_quota_negative as memo
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    clock = [1000.]
    monkeypatch.setattr(memo.time, 'time', lambda: clock[0])
    key = memo.key(token(), 'https://one.test/usage', 2000., 900.)
    memo.remember(key)
    assert memo.hit(key, 300)
    if change == 'token': key = memo.key(token('two'), 'https://one.test/usage', 2000., 900.)
    if change == 'url': key = memo.key(token(), 'https://two.test/usage', 2000., 900.)
    if change == 'reset': key = memo.key(token(), 'https://one.test/usage', 3000., 900.)
    if change == 'failure': key = memo.key(token(), 'https://one.test/usage', 2000., 950.)
    if change == 'home': monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'other'))
    if change == 'ttl': clock[0] += 60
    if change == 'future': clock[0] -= 1
    if change == 'corrupt': memo._path().write_text('{invalid')
    assert not memo.hit(key, 300)


@pytest.mark.parametrize('status,result', [(200, True), (401, None), (500, None)])
def test_positive_and_unknown_results_are_not_shared(tmp_path, monkeypatch, status, result):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    calls = []
    @contextmanager
    def client(**kwargs):
        calls.append(1)
        payload = {'rate_limit': {'primary_window': {'used_percent': 0}, 'secondary_window': {'used_percent': 0}}}
        yield SimpleNamespace(get=lambda *a, **k: SimpleNamespace(status_code=status, json=lambda: payload))
    monkeypatch.setattr(auth_codex, '_codex_http_client', client)
    for _ in range(2):
        auth._codex_quota_probe_cache.clear()
        assert auth_codex._probe_codex_quota_restored(token()) is result
    assert len(calls) == 2


def test_cached_negative_never_selects_exhausted_account(tmp_path, monkeypatch):
    import time
    from agent.credential_pool import CredentialPool, PooledCredential
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    calls = []
    @contextmanager
    def client(**kwargs):
        calls.append(1)
        yield SimpleNamespace(get=lambda *a, **k: SimpleNamespace(status_code=429))
    monkeypatch.setattr(auth_codex, '_codex_http_client', client)
    now = time.time()
    row = dict(id='exhausted', label='exhausted', auth_type='oauth', source='manual:device_code',
               access_token=token(), last_status='exhausted', last_status_at=now,
               last_error_code=429, last_error_reason='usage_limit_reached', last_error_reset_at=now + 3600)
    (tmp_path / 'auth.json').write_text(json.dumps({'version': 1, 'credential_pool': {'openai-codex': [row]}}))
    for _ in range(3):
        auth._codex_quota_probe_cache.clear()
        pool = CredentialPool('openai-codex', [PooledCredential.from_dict('openai-codex', row)])
        assert pool.select() is None
        assert pool.entries()[0].last_status == 'exhausted'
    assert len(calls) == 1
