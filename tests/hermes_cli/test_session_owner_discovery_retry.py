"""A session write rides out a briefly stalled gateway owner without a second mutation.

Hub, Nov 11: the gateway's control loop blocked for about a second, one 1.5 s ``identify`` answered
``control_timeout``, and Desktop's archive came back as a 503 telling the user to drain and stop
the live gateway. Discovery now re-reads a ``control_timeout`` (read-only, before anything is
sent) inside one bounded budget; every other verdict stays final on its first read, the offline
lock still decides an owner-less home, and the mutation is sent at most once per request.
"""
from contextlib import asynccontextmanager, contextmanager
import asyncio
import os
import signal
import sqlite3
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from hermes_cli import session_owner_discovery as sod
from hermes_cli.gateway_runtime import GatewayDiscovery

TIMEOUT = GatewayDiscovery('inaccessible', reason_code='control_timeout')


class _Clock:
    """Monotonic time the fake reads and sleeps advance; no test waits on the wall clock."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


def _reads(monkeypatch, verdicts, clock=None, cost=None):
    """Discovery answers *verdicts* in order (the last repeats); each read records its timeout."""
    seen = []

    def fake(home, timeout=2.0):
        seen.append(timeout)
        if clock is not None:
            clock.now += timeout if cost is None else cost(timeout)
        return verdicts[min(len(seen), len(verdicts)) - 1]
    monkeypatch.setattr('hermes_cli.gateway_runtime.discover_gateway_endpoint', fake)
    return seen


@pytest.mark.asyncio
async def test_a_control_timeout_is_re_read_until_the_owner_answers(tmp_path, monkeypatch):
    ready = SimpleNamespace(state='ready', endpoint='owner', reason_code=None, detail=None)
    clock = _Clock()
    seen = _reads(monkeypatch, [TIMEOUT, ready], clock)
    assert await sod.discover_session_owner(tmp_path, clock=clock, sleep=clock.sleep) is ready
    assert seen == [1.5, 1.5]


@pytest.mark.asyncio
async def test_repeated_stalls_stop_within_the_budget(tmp_path, monkeypatch):
    """Every read spends its whole deadline: three reads, never past six seconds."""
    clock = _Clock()
    start = clock.now
    seen = _reads(monkeypatch, [TIMEOUT], clock)
    verdict = await sod.discover_session_owner(tmp_path, clock=clock, sleep=clock.sleep)
    assert verdict.reason_code == 'control_timeout' and verdict.endpoint is None
    assert len(seen) == sod.OWNER_DISCOVERY_ATTEMPTS == 3
    assert clock.now - start <= sod.OWNER_DISCOVERY_BUDGET_S <= 6.0
    assert all(0 < timeout <= 1.5 for timeout in seen)


@pytest.mark.asyncio
async def test_overrunning_reads_shrink_to_the_remaining_budget(tmp_path, monkeypatch):
    """A read that overruns its own deadline (the incident's loop stall) leaves less for the next;
    one is never started with less than the minimum left, and none gets a timeout <= 0."""
    clock = _Clock()
    start = clock.now
    seen = _reads(monkeypatch, [TIMEOUT], clock, cost=lambda timeout: timeout + 1.0)
    await sod.discover_session_owner(tmp_path, clock=clock, sleep=clock.sleep)
    assert seen[0] == 1.5 and seen[-1] < 1.5 and all(timeout > 0 for timeout in seen)
    # The last read was capped by the remaining budget; only its own overrun lies past it.
    assert clock.now - start - 1.0 <= sod.OWNER_DISCOVERY_BUDGET_S + 1e-9


@pytest.mark.asyncio
async def test_a_read_too_late_to_fit_is_not_started(tmp_path, monkeypatch):
    clock = _Clock()
    seen = _reads(monkeypatch, [TIMEOUT], clock, cost=lambda _timeout: 5.8)
    await sod.discover_session_owner(tmp_path, clock=clock, sleep=clock.sleep)
    assert seen == [1.5]


@pytest.mark.asyncio
async def test_a_read_stuck_past_its_socket_deadline_cannot_stretch_the_write(tmp_path, monkeypatch):
    """The budget is enforced from the request's side: a read thread that ignores its timeout is
    abandoned at the deadline and counts as another control_timeout."""
    monkeypatch.setattr(sod, 'OWNER_DISCOVERY_BUDGET_S', 0.4)
    release = threading.Event()
    calls = []

    def stuck(home, timeout=2.0):
        calls.append(timeout)
        release.wait(10)
        return GatewayDiscovery('absent')
    monkeypatch.setattr('hermes_cli.gateway_runtime.discover_gateway_endpoint', stuck)
    try:
        started = time.monotonic()
        verdict = await sod.discover_session_owner(tmp_path)
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert verdict.reason_code == 'control_timeout'
    assert len(calls) == 1 and 0 < calls[0] <= 0.4  # the next read would not fit in what is left
    assert elapsed < 2.0


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict', [
    GatewayDiscovery('absent'),
    GatewayDiscovery('starting'),
    GatewayDiscovery('draining', reason_code='update_paused'),
    GatewayDiscovery('inaccessible', reason_code='authorization'),
    GatewayDiscovery('inaccessible', reason_code='profile_mismatch'),
    GatewayDiscovery('inaccessible', reason_code='unsafe_control_permissions'),
    GatewayDiscovery('inaccessible', reason_code='invalid_control_peer'),
    GatewayDiscovery('incompatible', reason_code='runtime_protocol'),
])
async def test_every_other_verdict_is_final_on_its_first_read(tmp_path, monkeypatch, verdict):
    clock = _Clock()
    seen = _reads(monkeypatch, [verdict, TIMEOUT], clock)
    assert await sod.discover_session_owner(tmp_path, clock=clock, sleep=clock.sleep) is verdict
    assert len(seen) == 1


def test_a_timeout_with_an_endpoint_is_not_retried():
    assert not sod.is_transient(SimpleNamespace(state='inaccessible', reason_code='control_timeout',
                                                endpoint='owner'))
    assert sod.is_transient(TIMEOUT)


def _standalone_request(monkeypatch, tmp_path):
    monkeypatch.setattr('hermes_state._default_db_path', lambda: str(tmp_path / 'state.db'))
    return SimpleNamespace(
        state=SimpleNamespace(native_http_principal=None,
                              session=SimpleNamespace(user_id='user-a', provider='oidc',
                                                      issuer='https://idp.example')),
        app=SimpleNamespace(state=SimpleNamespace(auth_required=True, session_authority=None,
                                                  gateway_runner=None)))


def _no_backoff(monkeypatch):
    monkeypatch.setattr(sod, 'OWNER_DISCOVERY_BACKOFF_S', 0.0)


@pytest.mark.asyncio
async def test_initial_timeout_then_healthy_owner_sends_exactly_one_mutation(tmp_path, monkeypatch):
    from hermes_cli import web_server_sessions

    _no_backoff(monkeypatch)
    request = _standalone_request(monkeypatch, tmp_path)
    ready = SimpleNamespace(state='ready', endpoint='owner-endpoint', reason_code=None, detail=None)
    seen = _reads(monkeypatch, [TIMEOUT, ready])
    rpcs, offline = [], []

    class Client:
        async def rpc(self, method, **params):
            rpcs.append((method, params))
            return {'session_id': params['session_id'], 'operation': 'sidebar', 'revision': 1}

    @asynccontextmanager
    async def connect(endpoint=None, subject=None):
        assert endpoint == 'owner-endpoint'
        yield Client()
    monkeypatch.setattr('hermes_cli.gateway_client.connect_gateway', connect)
    monkeypatch.setattr(web_server_sessions, '_mutate_offline_store',
                        lambda profile, actor, params: offline.append(params))
    result = await web_server_sessions._mutate_session_request(
        request, None, 's1', request_id='req-1', expected_revision=0,
        operation='sidebar', payload={'archived': True, 'pinned': False}, expected_generation=0)
    assert result == {'ok': True, 'session_id': 's1', 'operation': 'sidebar', 'revision': 1}
    assert len(seen) == 2
    assert [method for method, _ in rpcs] == ['session.mutate']
    assert rpcs[0][1]['request_id'] == 'req-1'
    assert offline == []


@pytest.mark.asyncio
async def test_an_ambiguous_forward_is_never_resent_by_the_server(tmp_path, monkeypatch):
    """A forward that dies after it was sent may have committed: one send, a retryable 503, and the
    client's retry replays its own request_id; the server never mints or resends one."""
    from hermes_cli import web_server_sessions

    _no_backoff(monkeypatch)
    request = _standalone_request(monkeypatch, tmp_path)
    ready = SimpleNamespace(state='ready', endpoint='owner-endpoint', reason_code=None, detail=None)
    _reads(monkeypatch, [TIMEOUT, ready])
    rpcs, offline = [], []

    class Client:
        async def rpc(self, method, **params):
            rpcs.append(params)
            raise TimeoutError('reply lost after send')

    @asynccontextmanager
    async def connect(endpoint=None, subject=None):
        yield Client()
    monkeypatch.setattr('hermes_cli.gateway_client.connect_gateway', connect)
    monkeypatch.setattr(web_server_sessions, '_mutate_offline_store',
                        lambda profile, actor, params: offline.append(params))
    with pytest.raises(HTTPException) as exc:
        await web_server_sessions._mutate_session_request(
            request, None, 's1', request_id='req-1', expected_revision=0,
            operation='sidebar', payload={'archived': True})
    assert (exc.value.status_code, exc.value.detail) == (503, 'owner_unreachable')
    assert [params['request_id'] for params in rpcs] == ['req-1']
    assert offline == []


@pytest.mark.asyncio
async def test_exhausted_stalls_behind_a_held_lock_answer_retry_not_drain(tmp_path, monkeypatch):
    """The owner still holds the lock: the offline write loses (never bypassed), and the 503 asks for
    a retry instead of telling the user to drain and stop a live gateway."""
    from gateway.runtime_ownership import OwnershipConflict
    from hermes_cli import web_server_sessions

    _no_backoff(monkeypatch)
    request = _standalone_request(monkeypatch, tmp_path)
    seen = _reads(monkeypatch, [TIMEOUT])
    attempts = []

    @contextmanager
    def held(homes):
        attempts.append(homes)
        raise OwnershipConflict('Exclusive maintenance refused: held. Drain and stop the gateway, then retry.')
        yield
    monkeypatch.setattr('gateway.runtime_ownership.exclusive_maintenance', held)
    with pytest.raises(HTTPException) as exc:
        await web_server_sessions._mutate_session_request(
            request, None, 's1', request_id='req-1', expected_revision=0,
            operation='sidebar', payload={'archived': True})
    assert exc.value.status_code == 503
    assert exc.value.detail == sod.CONTROL_TIMEOUT_DETAIL
    assert 'Drain' not in exc.value.detail and 'stop' not in exc.value.detail
    assert len(seen) == 3 and len(attempts) == 1


@pytest.mark.asyncio
async def test_an_owner_gone_by_the_last_read_still_edits_offline(tmp_path, monkeypatch):
    """Stalls then no owner at all: the free lock lets the offline write land, as on main."""
    from hermes_cli import web_server_sessions

    _no_backoff(monkeypatch)
    request = _standalone_request(monkeypatch, tmp_path)
    seen = _reads(monkeypatch, [TIMEOUT, TIMEOUT, GatewayDiscovery('absent')])
    monkeypatch.setattr(web_server_sessions, '_mutate_offline_store', lambda profile, actor, params:
                        {'session_id': params['session_id'], 'operation': 'sidebar', 'revision': 1})
    result = await web_server_sessions._mutate_session_request(
        request, None, 's1', request_id='req-1', expected_revision=0,
        operation='sidebar', payload={'pinned': True})
    assert result['revision'] == 1 and len(seen) == 3


def test_live_owner_forward_rereads_a_timeout_before_forwarding(tmp_path, monkeypatch):
    """``_on_live_owner`` (mute, owner-backfill) shares the same discovery."""
    from contextlib import contextmanager as cm

    import gateway.runtime_ownership as ownership
    import hermes_state
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli.web_routers.sessions import manage_router

    _no_backoff(monkeypatch)
    home = tmp_path / 'state'
    home.mkdir(mode=0o700)
    seen = _reads(monkeypatch, [TIMEOUT])

    @cm
    def held(_homes, **_kw):
        raise ownership.OwnershipConflict('Exclusive maintenance refused: held. Drain and stop the gateway, then retry.')
        yield
    monkeypatch.setattr(ownership, 'exclusive_maintenance', held)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', home / 'state.db')
    app = FastAPI()
    app.include_router(manage_router)
    with TestClient(app) as client:
        refused = client.post('/api/sessions/owner-backfill', json={})
    assert refused.status_code == 503, refused.text
    assert refused.json()['detail'] == sod.CONTROL_TIMEOUT_DETAIL
    assert len(seen) == 3


# --- An isolated real gateway, stalled with SIGSTOP (never a live one). ---

AUTH = {'Authorization': 'Bearer standalone-token'}


def _standalone_app(home, monkeypatch):
    import hermes_state
    from fastapi import FastAPI
    from hermes_cli import web_server
    from hermes_cli.web_routers.sessions import manage_router

    monkeypatch.setattr(web_server, '_SESSION_TOKEN', 'standalone-token')
    monkeypatch.setattr('hermes_state._default_db_path', lambda: home / 'state.db')
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', home / 'state.db')
    monkeypatch.setenv('HERMES_HOME', str(home))
    app = FastAPI()
    app.include_router(manage_router)
    return app


def _gateway_pid(home):
    import json
    return int(json.loads((home / 'gateway.pid').read_text())['pid'])


def _row(home):
    with sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True) as raw:
        return raw.execute("SELECT archived, runtime_revision FROM sessions WHERE id = 'chat'").fetchone()


@pytest.mark.platforms('linux')
@pytest.mark.asyncio
async def test_a_stalled_real_owner_takes_the_write_once_it_answers(tmp_path, monkeypatch):
    from hermes_cli import gateway_runtime
    from tests.gateway.test_mute_live_owner import _live_gateway

    with _live_gateway(tmp_path) as (home, _descriptor):
        pid = _gateway_pid(home)
        real = gateway_runtime.discover_gateway_endpoint
        reads = []

        def stalled_then_resumed(path, timeout=2.0):
            verdict = real(path, timeout=timeout)
            reads.append(verdict.reason_code or verdict.state)
            if verdict.reason_code == 'control_timeout' and len(reads) == 1:
                os.kill(pid, signal.SIGCONT)  # the stall ends after the first read missed it
            return verdict
        monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', stalled_then_resumed)
        app = _standalone_app(home, monkeypatch)
        body = {'request_id': 'archive-1', 'expected_revision': 0, 'expected_generation': 0,
                'archived': True, 'pinned': False}
        os.kill(pid, signal.SIGSTOP)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url='http://localhost', headers=AUTH, timeout=30) as client:
                done = await client.patch('/api/sessions/chat', json=body)
                assert done.status_code == 200, done.text
                assert reads[0] == 'control_timeout' and reads[-1] == 'ready'
                assert done.json()['revision'] == 1
                assert _row(home) == (1, 1)
                # The same intent replayed (a client retry after a lost reply) is the same receipt.
                again = await client.patch('/api/sessions/chat', json=body)
                assert again.json() == done.json()
                assert _row(home) == (1, 1)
        finally:
            os.kill(pid, signal.SIGCONT)


@pytest.mark.platforms('linux')
@pytest.mark.asyncio
async def test_a_real_owner_stalled_past_the_budget_refuses_then_retry_lands_once(tmp_path, monkeypatch):
    from tests.gateway.test_mute_live_owner import _live_gateway

    with _live_gateway(tmp_path) as (home, _descriptor):
        pid = _gateway_pid(home)
        app = _standalone_app(home, monkeypatch)
        body = {'request_id': 'archive-2', 'expected_revision': 0, 'expected_generation': 0,
                'archived': True, 'pinned': False}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://localhost', headers=AUTH, timeout=30) as client:
            os.kill(pid, signal.SIGSTOP)
            try:
                started = time.monotonic()
                refused = await client.patch('/api/sessions/chat', json=body)
                elapsed = time.monotonic() - started
            finally:
                os.kill(pid, signal.SIGCONT)
            assert refused.status_code == 503, refused.text
            assert refused.json()['detail'] == sod.CONTROL_TIMEOUT_DETAIL
            # Bounded: discovery's budget plus the offline lock's immediate refusal.
            assert elapsed < sod.OWNER_DISCOVERY_BUDGET_S + 1.5, elapsed
            # The held lock was never bypassed: nothing changed while the owner was stalled.
            assert _row(home) == (0, 0)
            retried = await client.patch('/api/sessions/chat', json=body)
            assert retried.status_code == 200, retried.text
            assert retried.json()['revision'] == 1
            assert _row(home) == (1, 1)


def test_the_budget_constants_stay_bounded():
    worst = (sod.OWNER_DISCOVERY_ATTEMPTS * sod.OWNER_DISCOVERY_ATTEMPT_S
             + (sod.OWNER_DISCOVERY_ATTEMPTS - 1) * sod.OWNER_DISCOVERY_BACKOFF_S)
    assert worst <= sod.OWNER_DISCOVERY_BUDGET_S <= 6.0
    assert asyncio.iscoroutinefunction(sod.discover_session_owner)
