"""Completion proof, deadline custody, and startup retry for quota recovery."""
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from tests.agent import test_model_cooldown_recovery as pool_helpers

api_pool = pool_helpers.pool
MESSAGE = {'type': 'message', 'role': 'assistant', 'content': [{'type': 'text', 'text': 'O'}],
           'stop_reason': 'max_tokens'}


@pytest.fixture
def pool(api_pool):
    from agent.credential_pool import load_pool
    from hermes_cli import auth
    with auth._auth_store_lock():
        store = auth._load_auth_store()
        store['credential_pool']['anthropic'][0]['auth_type'] = 'oauth'
        auth._save_auth_store(store)
    return load_pool('anthropic', allowed_auth_types={'oauth'})


def _entry(**kw):
    return SimpleNamespace(provider='anthropic', auth_type='oauth', runtime_api_key='test-secret',
                           last_status='ready', last_error_code=None, last_error_reason=None,
                           model_cooldowns={'model': 9999999999}, extra={}, **kw)


def test_completion_uses_claude_code_wire(monkeypatch):
    from hermes_cli import kanban_quota_recovery as recovery
    seen = []
    def handle(request):
        import json
        seen.append((request, json.loads(request.content)))
        return httpx.Response(200, json=MESSAGE)
    monkeypatch.setattr('agent.anthropic_adapter._claude_code_version_cache', '9.8.7')
    monkeypatch.setattr(recovery, '_client', lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    assert recovery.completion_succeeds('anthropic', 'model', _entry(), recovery.time.monotonic() + 5)
    request, body = seen[0]
    assert request.url == 'https://api.anthropic.com/v1/messages'
    assert request.headers['x-app'] == 'cli'
    assert 'oauth-2025-04-20' in request.headers['anthropic-beta']
    assert request.headers['user-agent'] == 'claude-code/9.8.7 (external, cli)'
    assert body['max_tokens'] == 1 and body['model'] == 'model'
    assert body['system'][0]['text'] == "You are Claude Code, Anthropic's official CLI for Claude."
    assert len(seen) == 1


def test_proof_wire_headers_equal_the_oauth_client(monkeypatch):
    """The proof sends exactly the identity/beta headers the real OAuth client sends."""
    from agent import anthropic_adapter
    from hermes_cli import kanban_quota_recovery as recovery
    monkeypatch.setattr(anthropic_adapter, '_claude_code_version_cache', '9.8.7')
    for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL'):
        monkeypatch.delenv(name, raising=False)
    client = anthropic_adapter.build_anthropic_client('sk-ant-oat01-' + 'x' * 40)
    real = {k.lower(): v for k, v in client._custom_headers.items() if isinstance(v, str)}
    seen = []
    monkeypatch.setattr(recovery, '_client', lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: seen.append(request) or httpx.Response(200, json=MESSAGE))))
    assert recovery.completion_succeeds('anthropic', 'model', _entry(), recovery.time.monotonic() + 5)
    for header in ('anthropic-beta', 'user-agent', 'x-app'):
        assert seen[0].headers[header] == real[header], header


def _gated_entries(*ids):
    entries = [_entry(id=i) for i in ids]
    return entries, SimpleNamespace(entries=lambda: entries, clear_verified_model_cooldown=Mock(return_value=True))


def test_failed_proof_cools_down_per_account_across_starts(monkeypatch, tmp_path):
    """Two quota walls within the cooldown -> one real completion per (provider, model, account)."""
    from hermes_cli import kanban_quota_recovery as recovery
    monkeypatch.setattr(recovery, '_proof_gate_path', lambda: str(tmp_path / 'gate.db'))
    entries, pool = _gated_entries('acct1')
    monkeypatch.setattr('agent.credential_pool.load_pool', lambda *a, **k: pool)
    complete = Mock(return_value=False)
    monkeypatch.setattr(recovery, 'completion_succeeds', complete)
    clock = [1_000_000.0]
    monkeypatch.setattr(recovery.time, 'time', lambda: clock[0])
    assert not recovery.recover('anthropic', 'model')
    assert not recovery.recover('anthropic', 'model')
    assert complete.call_count == 1
    # Another model of the same account is its own proof.
    assert not recovery.recover('anthropic', 'other')
    assert complete.call_count == 1  # 'other' is not benched on the fixture entry, so ineligible
    entries[0].model_cooldowns['other'] = 9999999999
    assert not recovery.recover('anthropic', 'other')
    assert complete.call_count == 2
    clock[0] += recovery.FAILED_PROOF_COOLDOWN_SECONDS + 1
    assert not recovery.recover('anthropic', 'model')
    assert complete.call_count == 3


def test_verified_proof_releases_its_cooldown(monkeypatch, tmp_path):
    from hermes_cli import kanban_quota_recovery as recovery
    monkeypatch.setattr(recovery, '_proof_gate_path', lambda: str(tmp_path / 'gate.db'))
    monkeypatch.setattr('hermes_cli.kanban_quota_detector.record_contradiction', lambda *a, **k: None)
    _, pool = _gated_entries('acct1', 'acct2')
    monkeypatch.setattr('agent.credential_pool.load_pool', lambda *a, **k: pool)
    complete = Mock(side_effect=lambda p, m, e, d: e.id == 'acct1')
    monkeypatch.setattr(recovery, 'completion_succeeds', complete)
    assert recovery.recover('anthropic', 'model')
    assert recovery.recover('anthropic', 'model')
    # acct1 proved twice (released on success); acct2's failed proof stays benched.
    assert sorted(c.args[2].id for c in complete.call_args_list) == ['acct1', 'acct1', 'acct2']


def _gate_rows(path):
    import sqlite3
    from contextlib import closing
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as conn:
        return [row[0] for row in conn.execute('SELECT key FROM proofs')]


def test_expired_deadline_reserves_no_proof(monkeypatch, tmp_path):
    """A deadline already spent before the gate leaves no 600 s bench without a failed proof."""
    from hermes_cli import kanban_quota_recovery as recovery
    gate = tmp_path / 'gate.db'
    monkeypatch.setattr(recovery, '_proof_gate_path', lambda: str(gate))
    _, pool = _gated_entries('acct1')
    monkeypatch.setattr('agent.credential_pool.load_pool', lambda *a, **k: pool)
    complete = Mock(return_value=True)
    monkeypatch.setattr(recovery, 'completion_succeeds', complete)
    assert not recovery.recover('anthropic', 'model', seconds=0)
    assert _gate_rows(gate) == []
    complete.assert_not_called()


def test_deadline_lost_during_reservation_releases_rows(monkeypatch, tmp_path):
    """The deadline passing while the gate is reserved releases every unprobed reservation."""
    from hermes_cli import kanban_quota_recovery as recovery
    gate = tmp_path / 'gate.db'
    monkeypatch.setattr(recovery, '_proof_gate_path', lambda: str(gate))
    _, pool = _gated_entries('acct1', 'acct2')
    monkeypatch.setattr('agent.credential_pool.load_pool', lambda *a, **k: pool)
    complete = Mock(return_value=True)
    monkeypatch.setattr(recovery, 'completion_succeeds', complete)
    real_reserve = recovery._reserve_proofs

    def slow_reserve(*args, **kwargs):
        allowed = real_reserve(*args, **kwargs)
        assert len(_gate_rows(gate)) == 2
        late = time.monotonic() + 1000
        monkeypatch.setattr(recovery, 'time', SimpleNamespace(time=time.time, monotonic=lambda: late))
        return allowed

    monkeypatch.setattr(recovery, '_reserve_proofs', slow_reserve)
    assert not recovery.recover('anthropic', 'model', seconds=30)
    assert _gate_rows(gate) == []
    complete.assert_not_called()


def test_successful_late_proof_releases_its_row(monkeypatch, tmp_path):
    """A proof that succeeds after the deadline cannot clear the bench but must drop its reservation."""
    from hermes_cli import kanban_quota_recovery as recovery
    from tests.waiting import wait_until
    gate = tmp_path / 'gate.db'
    monkeypatch.setattr(recovery, '_proof_gate_path', lambda: str(gate))
    _, pool = _gated_entries('ok', 'walled')
    monkeypatch.setattr('agent.credential_pool.load_pool', lambda *a, **k: pool)
    release = threading.Event()
    returned = []

    def complete(provider, model, entry, deadline):
        release.wait(5)
        returned.append(entry.id)
        return entry.id == 'ok'

    monkeypatch.setattr(recovery, 'completion_succeeds', complete)
    try:
        assert not recovery.recover('anthropic', 'model', seconds=0.5)
        assert len(_gate_rows(gate)) == 2  # both proofs still in flight
    finally:
        release.set()
    wait_until(lambda: len(returned) == 2, what='late proofs to finish')
    wait_until(lambda: _gate_rows(gate) == [recovery._proof_key('anthropic', 'model', SimpleNamespace(id='walled'))],
               what='successful late proof to release its row')
    pool.clear_verified_model_cooldown.assert_not_called()


def test_broken_proof_gate_fails_open(monkeypatch, tmp_path):
    from hermes_cli import kanban_quota_recovery as recovery
    monkeypatch.setattr(recovery, '_proof_gate_path', lambda: str(tmp_path / 'missing-dir' / 'gate.db'))
    _, pool = _gated_entries('acct1')
    monkeypatch.setattr('agent.credential_pool.load_pool', lambda *a, **k: pool)
    complete = Mock(return_value=False)
    monkeypatch.setattr(recovery, 'completion_succeeds', complete)
    assert not recovery.recover('anthropic', 'model')
    assert not recovery.recover('anthropic', 'model')
    assert complete.call_count == 2


def test_failed_gate_release_logs_debug_and_never_raises(monkeypatch, caplog):
    """A locked gate in a proof thread's done-callback leaves its row, logged at DEBUG, not raised."""
    import logging
    import sqlite3
    from hermes_cli import kanban_quota_recovery as recovery

    def locked(path):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(recovery, '_gate_connect', locked)
    with caplog.at_level(logging.DEBUG, logger=recovery.__name__):
        recovery._release_proof('anthropic', 'model', _entry(id='acct1'), path='unused.db')
    [record] = [r for r in caplog.records if r.name == recovery.__name__]
    assert record.levelno == logging.DEBUG and 'release failed' in record.getMessage()
    assert record.exc_info and isinstance(record.exc_info[1], sqlite3.OperationalError)


@pytest.mark.parametrize('data,ok', [
    ('data: {"type":"response.created"}\n\n', False),
    ('data: {"type":"response.failed"}\n\n', False),
    ('data: {"type":"response.completed","response":{"status":"completed"}}\n\n', True),
    ('data: {"type":"response.completed","response":{"status":"failed"}}\n\n', False),
])
def test_codex_requires_completed(monkeypatch, data, ok):
    from hermes_cli import kanban_quota_recovery as recovery
    def handle(request):
        return httpx.Response(200, text=data)
    monkeypatch.setattr(recovery, '_client', lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    assert recovery.completion_succeeds('openai-codex', 'model', _entry(), recovery.time.monotonic() + 5) is ok


def test_recovery_cas_only_on_caller_and_success(monkeypatch):
    from hermes_cli import kanban_quota_recovery as recovery
    entries = [_entry(), _entry()]
    entries[1].runtime_api_key = 'other-secret'
    pool = SimpleNamespace(entries=lambda: entries, clear_verified_model_cooldown=Mock(return_value=True))
    monkeypatch.setattr('agent.credential_pool.load_pool', lambda *a, **k: pool)
    caller = threading.get_ident()
    def clear(observed, model):
        assert threading.get_ident() == caller
        return True
    pool.clear_verified_model_cooldown.side_effect = clear
    monkeypatch.setattr(recovery, 'completion_succeeds', lambda p, m, e, d: e.runtime_api_key == 'test-secret')
    assert recovery.recover('anthropic', 'model')
    pool.clear_verified_model_cooldown.assert_called_once_with(entries[0], 'model')


def test_late_completion_never_clears(monkeypatch):
    from hermes_cli import kanban_quota_recovery as recovery
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    pool = SimpleNamespace(entries=lambda: [_entry()], clear_verified_model_cooldown=Mock())
    monkeypatch.setattr('agent.credential_pool.load_pool', lambda *a, **k: pool)
    def complete(*args):
        started.set()
        release.wait(5)
        finished.set()
        return True
    monkeypatch.setattr(recovery, 'completion_succeeds', complete)
    try:
        assert not recovery.recover('anthropic', 'model', seconds=2.0)
        assert started.is_set()
    finally:
        release.set()
    assert finished.wait(5)
    pool.clear_verified_model_cooldown.assert_not_called()


@pytest.mark.parametrize('healed,second_fails', [(True, False), (True, True), (False, False)])
def test_startup_retries_once_without_double_probe(monkeypatch, healed, second_fails):
    from agent.managed_worker import _construct_agent
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    from hermes_cli import kanban_quota_recovery as recovery
    from gateway import session_kanban
    error = SubscriptionQuotaUnavailable('anthropic', 'model', 9999999999)
    factory = Mock(side_effect=[error, error if second_fails else 'agent'])
    probe = Mock(return_value=healed)
    monkeypatch.setattr(recovery, 'recover_exception', probe)
    monkeypatch.setattr('gateway.session_worker_construct.worker_route', lambda *a: {})
    booked = Mock()
    monkeypatch.setattr(session_kanban, 'record_start_failure', booked)
    frame = {'policy': {'kanban_json': '{}'}}
    if healed and not second_fails:
        assert _construct_agent(frame, factory, None) == 'agent'
        booked.assert_not_called()
    else:
        with pytest.raises(SubscriptionQuotaUnavailable):
            _construct_agent(frame, factory, None)
        booked.assert_called_once_with(frame, error)
        assert error._kanban_quota_probe_attempted
    assert factory.call_count == (2 if healed else 1)
    probe.assert_called_once_with(error)


@pytest.mark.parametrize('status', [401, 402, 403, 429, 500])
def test_rejected_completion_never_retries(monkeypatch, status):
    from hermes_cli import kanban_quota_recovery as recovery
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status)
    monkeypatch.setattr(recovery, '_client', lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    assert not recovery.completion_succeeds('anthropic', 'model', _entry(), recovery.time.monotonic() + 5)
    assert len(calls) == 1


@pytest.mark.parametrize('change', [
    {'auth_type': 'api_key'}, {'runtime_api_key': ''}, {'last_status': 'dead'},
    {'last_error_code': 401}, {'last_error_code': 402}, {'last_error_code': 403},
    {'last_error_reason': 'billing'}, {'extra': {'failure_reason': 'billing'}},
    {'model_cooldowns': {}},
])
def test_ineligible_rows_never_probe(monkeypatch, change):
    from hermes_cli import kanban_quota_recovery as recovery
    entry = _entry()
    for key, value in change.items():
        setattr(entry, key, value)
    pool = SimpleNamespace(entries=lambda: [entry], clear_verified_model_cooldown=Mock())
    monkeypatch.setattr('agent.credential_pool.load_pool', lambda *a, **k: pool)
    complete = Mock(return_value=True)
    monkeypatch.setattr(recovery, 'completion_succeeds', complete)
    assert not recovery.recover('anthropic', 'model')
    complete.assert_not_called()
    pool.clear_verified_model_cooldown.assert_not_called()


def test_default_contradiction_requires_persisted_recovery_and_skips_second_probe(monkeypatch):
    from hermes_cli import kanban_quota_probe as probe, kanban_quota_recovery as recovery
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable, QuotaContradicted
    error = SubscriptionQuotaUnavailable('anthropic', 'model', 9999999999)
    heal = Mock(return_value=False)
    monkeypatch.setattr(recovery, 'recover_exception', heal)
    assert probe.contradicted(error) is error
    heal.return_value = True
    assert isinstance(probe.contradicted(error), QuotaContradicted)
    heal.reset_mock()
    error._kanban_quota_probe_attempted = True
    assert probe.contradicted(error) is error
    heal.assert_not_called()


def test_completion_without_successful_cas_is_not_recovery(monkeypatch):
    from hermes_cli import kanban_quota_recovery as recovery
    pool = SimpleNamespace(entries=lambda: [_entry()], clear_verified_model_cooldown=Mock(return_value=False))
    monkeypatch.setattr('agent.credential_pool.load_pool', lambda *a, **k: pool)
    monkeypatch.setattr(recovery, 'completion_succeeds', lambda *args: True)
    assert not recovery.recover('anthropic', 'model')
    assert pool.clear_verified_model_cooldown.call_count == 1


def test_real_benched_pool_heals_and_startup_runs_without_usage_or_failure_booking(pool, monkeypatch):
    from agent.credential_pool import load_pool
    from agent.managed_worker import _construct_agent
    from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
    from hermes_cli.auth import read_credential_pool
    from hermes_cli import kanban_quota_recovery as recovery
    from gateway import session_kanban
    pool_helpers.bench(pool, pool_helpers.B)
    pool_helpers.bench(pool, pool_helpers.A)
    attempts = []
    def factory(**kw):
        attempts.append(kw)
        selected = load_pool('anthropic').select(model=pool_helpers.A)
        if selected is None:
            raise SubscriptionQuotaUnavailable('anthropic', pool_helpers.A, 9999999999)
        return selected
    monkeypatch.setattr('gateway.session_worker_construct.worker_route', lambda *args: {})
    monkeypatch.setattr('agent.account_usage.fetch_account_usage', lambda *a, **kw: pytest.fail('usage is not proof'))
    complete = Mock(return_value=True)
    monkeypatch.setattr(recovery, 'completion_succeeds', complete)
    booked = Mock()
    monkeypatch.setattr(session_kanban, 'record_start_failure', booked)
    selected = _construct_agent({'policy': {'kanban_json': '{}'}}, factory, None)
    assert selected.runtime_api_key == pool_helpers.KEY
    assert len(attempts) == 2
    assert complete.call_count == 1
    booked.assert_not_called()
    cooldowns = read_credential_pool('anthropic')[0]['model_cooldowns']
    assert pool_helpers.A not in cooldowns and pool_helpers.B in cooldowns


def test_disabled_account_is_not_probed(pool, monkeypatch):
    from agent.credential_pool_disabled import set_credential_disabled
    from hermes_cli import kanban_quota_recovery as recovery
    entry = pool_helpers.bench(pool)
    set_credential_disabled('anthropic', entry, True)
    complete = Mock(return_value=True)
    monkeypatch.setattr(recovery, 'completion_succeeds', complete)
    assert not recovery.recover('anthropic', pool_helpers.A)
    complete.assert_not_called()


def test_three_recoveries_record_accounts_and_owner_files_one_card(pool, monkeypatch, tmp_path):
    from hermes_cli import kanban_quota_recovery as recovery, kanban_quota_detector as detector
    from hermes_cli import kanban_db_connect as kb
    path = tmp_path / 'contradictions.db'
    monkeypatch.setattr(detector, 'default_path', lambda: str(path))
    monkeypatch.setattr(recovery, 'completion_succeeds', lambda *args: True)
    for _ in range(3):
        entry = pool_helpers.bench(pool)
        assert recovery.recover('anthropic', pool_helpers.A)
        # Re-load after the verified clear before recording a fresh failure.
        from agent.credential_pool import load_pool
        pool = load_pool('anthropic', allowed_auth_types={'oauth'})
    pending = detector.Detector(path).pending()
    assert len(pending) == 1
    assert pending[0]['count'] == 3
    assert pending[0]['accounts'][0]['id'] == entry.id
    assert pending[0]['accounts'][0]['cooldown_until'] > 0
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.setattr('agent.delegation_context.is_delegated_child_process_context', lambda: False)
    conn = kb.connect(tmp_path / 'board.db')
    try:
        assert detector.drain_owner(conn) == 1
        assert detector.drain_owner(conn) == 0
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
    finally:
        conn.close()



@pytest.mark.parametrize('content', [
    b'', b'{"type":"message","role":"assistant","content":[', b'not json', b'[]',
    b'{"type":"error","error":{"type":"overloaded_error"}}',
    b'{"type":"message","role":"assistant","content":[],"stop_reason":null}',
    b'{"content":[]}',
])
def test_anthropic_200_requires_valid_completed_body(monkeypatch, content):
    from hermes_cli import kanban_quota_recovery as recovery
    monkeypatch.setattr(recovery, '_client', lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content))))
    assert not recovery.completion_succeeds('anthropic', 'model', _entry(), recovery.time.monotonic() + 5)


@pytest.mark.parametrize('provider', ['anthropic', 'openai-codex'])
def test_response_bytes_are_bounded(monkeypatch, provider):
    from hermes_cli import kanban_quota_recovery as recovery
    monkeypatch.setattr(recovery, 'MAX_RESPONSE_BYTES', 1000, raising=False)
    big = (b'{"type":"message","role":"assistant","stop_reason":"end_turn","content":[],"x":"' + b'a' * 2000 + b'"}'
           if provider == 'anthropic' else b'data: ' + b'a' * 2000)
    monkeypatch.setattr(recovery, '_client', lambda timeout: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=big))))
    assert not recovery.completion_succeeds(provider, 'model', _entry(), recovery.time.monotonic() + 5)


_SERVER = textwrap.dedent(r'''
    import socket, sys, time
    mode = sys.argv[1]
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
    print(s.getsockname()[1], flush=True)
    c, _ = s.accept()
    c.recv(65536)
    head = b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n\r\n"
    if mode == "anthropic":
        head = b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: 100000\r\n\r\n"
    c.sendall(head)
    try:
        while True:
            c.sendall(b" ")  # trickle: never a newline, never a complete body
            time.sleep(0.05)
    except OSError:
        print("PEER_CLOSED", flush=True)
''')


@pytest.mark.parametrize('provider', ['anthropic', 'openai-codex'])
def test_trickling_stream_is_cancelled_at_deadline_and_peer_sees_close(monkeypatch, provider):
    from hermes_cli import kanban_quota_recovery as recovery
    proc = subprocess.Popen([sys.executable, '-c', _SERVER, provider], stdout=subprocess.PIPE, text=True)
    try:
        port = int(proc.stdout.readline())
        monkeypatch.setattr(recovery, '_client', lambda timeout: httpx.AsyncClient(timeout=30))
        real = httpx.AsyncClient.stream
        monkeypatch.setattr(httpx.AsyncClient, 'stream',
                            lambda self, method, url, **kw: real(self, method, f'http://127.0.0.1:{port}/', **kw))
        start = time.monotonic()
        assert not recovery.completion_succeeds(provider, 'model', _entry(), start + 1.0)
        assert time.monotonic() - start < 2.5
        assert proc.stdout.readline().strip() == 'PEER_CLOSED'
        assert proc.wait(5) == 0
    finally:
        proc.kill()
        proc.wait()
