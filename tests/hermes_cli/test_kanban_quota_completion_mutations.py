"""Behaviour-killing proofs for quota completion: completed body, absolute deadline, concurrent recover.

Every scenario speaks real TCP/HTTP to a local server (no MockTransport). Each "mutant" test
re-runs the same scenario with one production guard disabled and asserts the observable
outcome flips, so the guard is proven load-bearing rather than merely present.
"""
import json
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from hermes_cli import kanban_quota_recovery as recovery

MESSAGE = json.dumps({'type': 'message', 'role': 'assistant', 'content': [{'type': 'text', 'text': 'O'}],
                      'stop_reason': 'max_tokens'}).encode()
ERROR_BODY = b'{"type":"error","error":{"type":"overloaded_error"}}'
NO_STOP = b'{"type":"message","role":"assistant","content":[],"stop_reason":null}'
SSE_OK = b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
SSE_CREATED = b'data: {"type":"response.created"}\n\n'
SSE_FAILED_STATUS = b'data: {"type":"response.completed","response":{"status":"failed"}}\n\n'
SSE_FAILED_THEN_OK = (b'data: {"type":"response.failed"}\n\n' + SSE_OK)

_REAL_STREAM = httpx.AsyncClient.stream
JSON_HEAD = b'HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: %d\r\n\r\n'
SSE_HEAD = b'HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n'


class Server:
    """Threaded raw-socket server; behaviour is chosen by the bearer token (= mode name)."""

    def __init__(self, modes, trickle_limit=30.0):
        self.modes, self.trickle_limit = modes, trickle_limit
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.connected, self.closed, self.lock = {}, {}, threading.Lock()
        self.stop = threading.Event()
        self.sock.settimeout(0.1)  # accept() must wake to observe stop; close() alone may not
        self.threads, self._tlock = [], threading.Lock()
        self._spawn(self._accept)

    def _spawn(self, fn, *args):
        t = threading.Thread(target=fn, args=args, daemon=True)
        with self._tlock:
            self.threads.append(t)
        t.start()

    def _accept(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self._spawn(self._serve, conn)

    def _serve(self, conn):
        conn.settimeout(0.5)
        try:
            self._serve_inner(conn)
        except OSError:  # includes recv timeouts
            pass
        finally:
            conn.close()

    def _serve_inner(self, conn):
        data = b''
        while b'\r\n\r\n' not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return
            data += chunk
        head, _, rest = data.partition(b'\r\n\r\n')
        want = next((int(l.split(b':')[1]) for l in head.split(b'\r\n')
                     if l.lower().startswith(b'content-length')), 0)
        while len(rest) < want:  # drain the request body so close() never RSTs the response
            chunk = conn.recv(65536)
            if not chunk:
                return
            rest += chunk
        token = next(l.split(b'Bearer ')[1].decode().strip() for l in data.split(b'\r\n')
                     if l.lower().startswith(b'authorization'))
        with self.lock:
            self.connected[token] = time.monotonic()
            self.closed[token] = threading.Event()
        self.modes[token](self, conn, token)

    def trickle(self, conn, token, head):
        conn.sendall(head)
        end = time.monotonic() + self.trickle_limit
        try:
            while time.monotonic() < end:
                conn.sendall(b' ')
                if self.stop.wait(0.03):
                    return
        except OSError:
            self.closed[token].set()  # peer hung up on us
            return
        # Never saw a peer close within the limit.

    def close(self):
        self.stop.set()
        self.sock.close()
        with self._tlock:
            threads = list(self.threads)
        for t in threads:
            t.join(5)
        assert not any(t.is_alive() for t in threads), 'server thread leaked'


def _json(body, delay=0.0):
    def mode(srv, conn, token):
        srv.stop.wait(delay)
        conn.sendall(JSON_HEAD % len(body) + body)
        srv.stop.wait(0.05)
    return mode


def _sse(body, delay=0.0):
    def mode(srv, conn, token):
        srv.stop.wait(delay)
        conn.sendall(SSE_HEAD + body)
        srv.stop.wait(0.05)
    return mode


def _trickle_json(srv, conn, token):
    srv.trickle(conn, token, JSON_HEAD % 100000)


def _trickle_sse(srv, conn, token):
    srv.trickle(conn, token, SSE_HEAD)


@pytest.fixture
def serve(monkeypatch):
    servers = []

    def start(modes, **kw):
        srv = Server(modes, **kw)
        servers.append(srv)
        monkeypatch.setattr(recovery, '_client', lambda timeout: httpx.AsyncClient(timeout=30))
        monkeypatch.setattr(httpx.AsyncClient, 'stream', lambda self, method, url, **kw2: _REAL_STREAM(
            self, method, f'http://127.0.0.1:{srv.port}/', **kw2))
        return srv
    yield start
    for s in servers:
        s.close()


def _entry(key):
    return SimpleNamespace(id=key, provider='anthropic', auth_type='oauth', runtime_api_key=key,
                           last_status='ready', last_error_code=None, last_error_reason=None,
                           model_cooldowns={'m': 9999999999}, extra={})


def _probe(serve, provider, key, mode, seconds=2.0, **kw):
    serve({key: mode}, **kw)
    start = time.monotonic()
    ok = recovery.completion_succeeds(provider, 'm', _entry(key), start + seconds)
    return ok, time.monotonic() - start


# ---------------------------------------------------------------- completed body

BODY_CASES = [
    ('anthropic', 'ok', _json(MESSAGE), True),
    ('anthropic', 'err', _json(ERROR_BODY), False),
    ('anthropic', 'nostop', _json(NO_STOP), False),
    ('openai-codex', 'ok', _sse(SSE_OK), True),
    ('openai-codex', 'created', _sse(SSE_CREATED), False),
    ('openai-codex', 'badstatus', _sse(SSE_FAILED_STATUS), False),
    ('openai-codex', 'failed_then_ok', _sse(SSE_FAILED_THEN_OK), False),
]


@pytest.mark.parametrize('provider,key,mode,expected', BODY_CASES, ids=[c[1] + '-' + c[0] for c in BODY_CASES])
def test_real_socket_completed_body_decides_outcome(serve, provider, key, mode, expected):
    ok, _ = _probe(serve, provider, key, mode)
    assert ok is expected


@pytest.mark.parametrize('key,mutant,provider,mode', [
    ('err', ('_valid_message', lambda raw: True), 'anthropic', _json(ERROR_BODY)),
    ('nostop', ('_valid_message', lambda raw: True), 'anthropic', _json(NO_STOP)),
    ('created', ('_read_codex', None), 'openai-codex', _sse(SSE_CREATED)),
    ('badstatus', ('_read_codex', None), 'openai-codex', _sse(SSE_FAILED_STATUS)),
    ('failed_then_ok', ('_read_codex', None), 'openai-codex', _sse(SSE_FAILED_THEN_OK)),
])
def test_mutant_that_accepts_200_without_completed_body_is_killed(serve, monkeypatch, key, mutant, provider, mode):
    name, replacement = mutant
    if name == '_read_codex':
        async def replacement(response):  # mutant: any 200 stream that ends counts as proof
            async for _ in response.aiter_bytes():
                pass
            return True
    # Sanity: the real code rejects this exact wire exchange...
    ok, _ = _probe(serve, provider, key, mode)
    assert ok is False
    # ...and the mutant accepts it, so the body check above is what rejected it.
    monkeypatch.setattr(recovery, name, replacement)
    ok, _ = _probe(serve, provider, key, mode)
    assert ok is True


# ---------------------------------------------------------------- absolute deadline

@pytest.mark.parametrize('provider,mode', [('anthropic', _trickle_json), ('openai-codex', _trickle_sse)])
def test_trickle_cut_at_absolute_deadline_and_peer_sees_close(serve, provider, mode):
    srv_modes = {'t': mode}
    srv = serve(srv_modes)
    start = time.monotonic()
    assert recovery.completion_succeeds(provider, 'm', _entry('t'), start + 0.8) is False
    assert time.monotonic() - start < 2.0
    assert srv.closed['t'].wait(3)


@pytest.mark.parametrize('provider,mode', [('anthropic', _trickle_json), ('openai-codex', _trickle_sse)])
def test_mutant_without_absolute_deadline_overruns_and_is_killed(serve, monkeypatch, provider, mode):
    async def no_deadline(provider_, url, headers, body, deadline):
        async with recovery._client(30) as client, client.stream('POST', url, headers=headers, json=body) as r:
            if r.status_code != 200:
                return False
            return await (recovery._read_anthropic if provider_ == 'anthropic' else recovery._read_codex)(r)
    monkeypatch.setattr(recovery, '_attempt', no_deadline)
    serve({'t': mode}, trickle_limit=3.0)  # peer gives up after 3s, bounding the mutant run
    start = time.monotonic()
    assert recovery.completion_succeeds(provider, 'm', _entry('t'), start + 0.8) is False
    assert time.monotonic() - start > 2.5  # real code returns < 2.0 (test above)


@pytest.mark.parametrize('provider,mode', [('anthropic', _json(MESSAGE, delay=1.2)), ('openai-codex', _sse(SSE_OK, delay=1.2))])
def test_valid_body_arriving_after_deadline_is_not_proof(serve, monkeypatch, provider, mode):
    serve({'late': mode})
    start = time.monotonic()
    assert recovery.completion_succeeds(provider, 'm', _entry('late'), start + 0.5) is False
    assert time.monotonic() - start < 1.1  # cancelled at the deadline, not after the body
    # Mutant: no deadline at all -> the stale completion is accepted.
    async def no_deadline(provider_, url, headers, body, deadline):
        async with recovery._client(30) as client, client.stream('POST', url, headers=headers, json=body) as r:
            return r.status_code == 200 and await (
                recovery._read_anthropic if provider_ == 'anthropic' else recovery._read_codex)(r)
    monkeypatch.setattr(recovery, '_attempt', no_deadline)
    assert recovery.completion_succeeds(provider, 'm', _entry('late'), time.monotonic() + 0.5) is True


def test_expired_deadline_makes_no_connection(serve):
    srv = serve({'x': _json(MESSAGE)})
    assert recovery.completion_succeeds('anthropic', 'm', _entry('x'), time.monotonic() - 1) is False
    assert not srv.connected


# ---------------------------------------------------------------- concurrent recover

def _pool(entries):
    return SimpleNamespace(entries=lambda: entries, clear_verified_model_cooldown=Mock(return_value=True))


def _concurrent_modes():
    """fast answers only once slow is also connected: serial probing cannot pass."""
    def fast(srv, conn, token):
        deadline = time.monotonic() + 3
        while 'slow' not in srv.connected and time.monotonic() < deadline:
            srv.stop.wait(0.01)
        _json(MESSAGE)(srv, conn, token)
    return {'fast': fast, 'slow': _trickle_json}


def test_recover_probes_concurrently_clears_only_proven_and_cuts_slow_peer(serve, monkeypatch):
    srv = serve(_concurrent_modes())
    entries = [_entry('slow'), _entry('fast')]
    pool = _pool(entries)
    monkeypatch.setattr('agent.credential_pool.load_pool', lambda *a, **k: pool)
    monkeypatch.setattr('agent.credential_pool_disabled.disabled_keys', lambda p: set())
    monkeypatch.setattr(recovery, '_eligible', lambda *a: True)
    monkeypatch.setattr('hermes_cli.kanban_quota_detector.record_contradiction', lambda *a: None)
    start = time.monotonic()
    assert recovery.recover('anthropic', 'm', seconds=1.5) is True
    assert time.monotonic() - start < 2.5
    assert [c.args[0].runtime_api_key for c in pool.clear_verified_model_cooldown.call_args_list] == ['fast']
    assert srv.closed['slow'].wait(3)  # shared deadline closed the stalled socket


def test_mutant_serial_probing_is_killed(serve, monkeypatch):
    serve(_concurrent_modes())
    entries = [_entry('slow'), _entry('fast')]
    pool = _pool(entries)
    monkeypatch.setattr('agent.credential_pool.load_pool', lambda *a, **k: pool)
    monkeypatch.setattr('agent.credential_pool_disabled.disabled_keys', lambda p: set())
    monkeypatch.setattr(recovery, '_eligible', lambda *a: True)
    monkeypatch.setattr('hermes_cli.kanban_quota_detector.record_contradiction', lambda *a: None)
    real = recovery.ThreadPoolExecutor
    monkeypatch.setattr(recovery, 'ThreadPoolExecutor', lambda max_workers, **kw: real(max_workers=1, **kw))
    assert recovery.recover('anthropic', 'm', seconds=1.5) is False  # slow starves fast
    pool.clear_verified_model_cooldown.assert_not_called()


def test_recover_all_stalled_clears_nothing_within_deadline(serve, monkeypatch):
    serve({'slow': _trickle_json, 'slow2': _trickle_sse})
    pool = _pool([_entry('slow'), _entry('slow2')])
    monkeypatch.setattr('agent.credential_pool.load_pool', lambda *a, **k: pool)
    monkeypatch.setattr('agent.credential_pool_disabled.disabled_keys', lambda p: set())
    monkeypatch.setattr(recovery, '_eligible', lambda *a: True)
    start = time.monotonic()
    assert recovery.recover('anthropic', 'm', seconds=0.8) is False
    assert time.monotonic() - start < 2.0
    pool.clear_verified_model_cooldown.assert_not_called()


# ---------------------------------------------------------------- DNS executor finalization

@pytest.fixture
def hang_dns(monkeypatch, tmp_path):
    """Hang the actual resolver child, then verify cancellation reaped it."""
    from pathlib import Path

    baseline = set(threading.enumerate())
    marker = tmp_path / 'dns-pid'
    monkeypatch.setattr(recovery, '_DNS_SCRIPT', (
        "import os, time\n"
        f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(3)\n"))
    yield marker
    # recover may return just before its worker finishes cancellation. Join those
    # owned workers before restoring patches; never relax the caller bound.
    for thread in set(threading.enumerate()) - baseline:
        thread.join(1)
        assert not thread.is_alive()
    assert marker.exists(), 'resolver never started'
    pid = int(marker.read_text())
    assert not Path(f'/proc/{pid}').exists(), f'resolver {pid} not reaped'


def test_hung_dns_does_not_extend_completion_past_deadline(hang_dns):
    start = time.monotonic()
    assert recovery.completion_succeeds('anthropic', 'm', _entry('k'), start + 0.5) is False
    assert time.monotonic() - start < 1.5


def test_recover_stays_bounded_under_hung_dns_and_never_clears(monkeypatch, hang_dns):
    """recover() abandons workers at the shared deadline (shutdown(wait=False)), so the leak
    above delays only a detached thread, never the caller or a CAS."""
    pool = _pool([_entry('k')])
    monkeypatch.setattr('agent.credential_pool.load_pool', lambda *a, **k: pool)
    monkeypatch.setattr('agent.credential_pool_disabled.disabled_keys', lambda p: set())
    monkeypatch.setattr(recovery, '_eligible', lambda *a: True)
    start = time.monotonic()
    assert recovery.recover('anthropic', 'm', seconds=0.5) is False
    assert time.monotonic() - start < 1.5
    pool.clear_verified_model_cooldown.assert_not_called()
