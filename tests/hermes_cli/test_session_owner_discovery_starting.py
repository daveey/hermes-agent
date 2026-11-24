"""A session write rides out a gateway that is still starting, without a second mutation.

Hub, Nov 13: Desktop archives sent 4 s and 10 s after a gateway restart came back as a 503
``owner_unavailable: starting: Exclusive maintenance refused ... Drain and stop the gateway, then
retry``. The gateway held the profile's lock because it was coming up; the same request a few
seconds later landed. Discovery now re-reads ``starting`` (read-only, before anything is sent)
on its own budget, and a gateway still starting at the end of it answers a retryable 503 that
does not tell the user to stop it. ``absent`` and the refusal verdicts are unchanged.
"""
from contextlib import asynccontextmanager, contextmanager
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from hermes_cli import session_owner_discovery as sod
from hermes_cli.gateway_runtime import GatewayDiscovery
from tests.hermes_cli.test_session_owner_discovery_retry import (
    AUTH, TIMEOUT, _Clock, _no_backoff, _reads, _row, _standalone_app, _standalone_request)

STARTING = GatewayDiscovery('starting')
READY = SimpleNamespace(state='ready', endpoint='owner-endpoint', reason_code=None, detail=None)
DRAIN_ADVICE = 'Exclusive maintenance refused: held. Drain and stop the gateway, then retry.'


@pytest.mark.asyncio
async def test_starting_is_re_read_until_the_gateway_is_ready(tmp_path, monkeypatch):
    clock = _Clock()
    start = clock.now
    seen = _reads(monkeypatch, [STARTING] * 20 + [READY], clock, cost=lambda _timeout: 0.01)
    assert await sod.discover_session_owner(tmp_path, clock=clock, sleep=clock.sleep) is READY
    assert len(seen) == 21  # well past the three reads a control_timeout gets
    assert clock.now - start < sod.OWNER_STARTING_BUDGET_S


@pytest.mark.asyncio
async def test_a_persisting_starting_stops_within_its_budget(tmp_path, monkeypatch):
    clock = _Clock()
    start = clock.now
    seen = _reads(monkeypatch, [STARTING], clock, cost=lambda _timeout: 0.01)
    verdict = await sod.discover_session_owner(tmp_path, clock=clock, sleep=clock.sleep)
    assert verdict is STARTING
    assert sod.OWNER_STARTING_BUDGET_S - 1.0 < clock.now - start <= sod.OWNER_STARTING_BUDGET_S
    assert all(0 < timeout <= sod.OWNER_DISCOVERY_ATTEMPT_S for timeout in seen)


@pytest.mark.asyncio
async def test_identify_stalls_of_a_starting_gateway_are_re_read_on_the_starting_budget(tmp_path, monkeypatch):
    """A starting gateway's busy loop misses identify deadlines: once ``starting`` was seen, a
    ``control_timeout`` no longer ends discovery after three reads."""
    clock = _Clock()
    seen = _reads(monkeypatch, [TIMEOUT, STARTING, TIMEOUT, TIMEOUT, TIMEOUT, TIMEOUT, STARTING, READY], clock)
    assert await sod.discover_session_owner(tmp_path, clock=clock, sleep=clock.sleep) is READY
    assert len(seen) == 8


@pytest.mark.asyncio
async def test_the_starting_budget_counts_from_the_first_read(tmp_path, monkeypatch):
    """Stalls before the first ``starting`` are not added on top of the starting budget."""
    clock = _Clock()
    start = clock.now
    _reads(monkeypatch, [TIMEOUT, TIMEOUT, STARTING], clock,
           cost=lambda timeout: timeout if clock.now - start < 3.4 else 0.01)
    assert await sod.discover_session_owner(tmp_path, clock=clock, sleep=clock.sleep) is STARTING
    assert clock.now - start <= sod.OWNER_STARTING_BUDGET_S


@pytest.mark.asyncio
@pytest.mark.parametrize('after', [
    GatewayDiscovery('absent'),
    GatewayDiscovery('draining', reason_code='update_paused'),
    GatewayDiscovery('inaccessible', reason_code='authorization'),
    GatewayDiscovery('incompatible', reason_code='runtime_protocol'),
])
async def test_a_final_verdict_after_starting_ends_the_wait_at_once(tmp_path, monkeypatch, after):
    """A stopping gateway's lock reads ``starting`` for a moment, then ``absent``: that is final."""
    clock = _Clock()
    seen = _reads(monkeypatch, [STARTING, STARTING, after, READY], clock, cost=lambda _timeout: 0.01)
    assert await sod.discover_session_owner(tmp_path, clock=clock, sleep=clock.sleep) is after
    assert len(seen) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('settling', [STARTING, GatewayDiscovery('draining')])
async def test_a_restart_budget_ending_on_a_stall_answers_the_restart(tmp_path, monkeypatch, settling):
    """Hub, 2026-11-24 16:00Z: a core-update restart took over 40 s and its loop stalled 1.5-4.8 s,
    so an archive saw ``starting`` and then only missed identifies until the budget ran out. That
    write must answer the restart verdict (Desktop replays it), not ``control_timeout`` (final)."""
    clock = _Clock()
    start = clock.now
    _reads(monkeypatch, [settling, TIMEOUT], clock)
    verdict = await sod.discover_session_owner(tmp_path, clock=clock, sleep=clock.sleep)
    assert verdict is settling
    assert sod.owner_unavailable_detail(verdict, DRAIN_ADVICE) != sod.CONTROL_TIMEOUT_DETAIL
    assert clock.now - start <= sod.OWNER_STARTING_BUDGET_S


@pytest.mark.asyncio
async def test_the_newest_restart_verdict_is_the_one_answered(tmp_path, monkeypatch, caplog):
    clock = _Clock()
    _reads(monkeypatch, [GatewayDiscovery('draining'), STARTING, TIMEOUT], clock)
    with caplog.at_level('INFO', logger=sod.__name__):
        assert await sod.discover_session_owner(tmp_path, clock=clock, sleep=clock.sleep) is STARTING
    assert 'last read timed out mid-restart' in caplog.text


@pytest.mark.asyncio
async def test_a_stall_without_a_restart_seen_stays_control_timeout(tmp_path, monkeypatch):
    clock = _Clock()
    seen = _reads(monkeypatch, [TIMEOUT], clock)
    assert sod.is_transient(await sod.discover_session_owner(tmp_path, clock=clock, sleep=clock.sleep))
    assert len(seen) == sod.OWNER_DISCOVERY_ATTEMPTS


@pytest.mark.asyncio
@pytest.mark.parametrize('settling, detail', [(STARTING, sod.STARTING_DETAIL),
                                              (GatewayDiscovery('draining'), sod.DRAINING_DETAIL)])
async def test_a_restart_ending_on_stalls_answers_the_restart_503(tmp_path, monkeypatch, settling, detail):
    """The write path: the restart 503 that Desktop's restart replay matches, nothing forwarded."""
    from hermes_cli import web_server_sessions

    _fast_poll(monkeypatch)
    monkeypatch.setattr(sod, 'OWNER_STARTING_BUDGET_S', 0.3)
    request = _standalone_request(monkeypatch, tmp_path)
    _reads(monkeypatch, [settling, TIMEOUT])
    attempts, connects = [], []
    monkeypatch.setattr('hermes_cli.gateway_client.connect_gateway', lambda **kw: connects.append(kw))
    with _held(attempts) as held:
        monkeypatch.setattr('gateway.runtime_ownership.exclusive_maintenance', held)
        with pytest.raises(HTTPException) as exc:
            await web_server_sessions._mutate_session_request(
                request, None, 's1', request_id='req-1', expected_revision=0,
                operation='sidebar', payload={'archived': True})
    assert exc.value.status_code == 503
    assert exc.value.detail == detail
    assert len(attempts) == 1 and connects == []


def test_only_a_starting_verdict_without_an_endpoint_is_starting():
    assert sod.is_starting(STARTING) and sod.is_starting(GatewayDiscovery('starting', reason_code='deadline'))
    assert not sod.is_starting(SimpleNamespace(state='starting', endpoint='owner', reason_code=None))
    assert not sod.is_starting(GatewayDiscovery('absent')) and not sod.is_starting(TIMEOUT)
    assert not sod.is_transient(STARTING)


def test_the_detail_names_starting_only_for_starting():
    assert sod.owner_unavailable_detail(STARTING, DRAIN_ADVICE) == sod.STARTING_DETAIL
    assert sod.STARTING_DETAIL.startswith('owner_unavailable: starting: ')
    assert 'Drain' not in sod.STARTING_DETAIL and 'stop' not in sod.STARTING_DETAIL
    assert sod.owner_unavailable_detail(TIMEOUT, DRAIN_ADVICE) == sod.CONTROL_TIMEOUT_DETAIL
    draining = GatewayDiscovery('draining', reason_code='update_paused')
    assert sod.owner_unavailable_detail(draining, DRAIN_ADVICE) == f'owner_unavailable: update_paused: {DRAIN_ADVICE}'
    refused = GatewayDiscovery('inaccessible', reason_code='authorization')
    assert sod.owner_unavailable_detail(refused, DRAIN_ADVICE) == f'owner_unavailable: authorization: {DRAIN_ADVICE}'


def _desktop_request_timeout_s():
    """Desktop's default request deadline, read from its source: a renamed or re-spelled constant
    fails here instead of silently unpinning the budget below."""
    import re

    source = Path(__file__).resolve().parents[2] / 'apps' / 'desktop' / 'electron' / 'hardening.ts'
    found = re.search(r'^const DEFAULT_FETCH_TIMEOUT_MS = ([0-9_]+)$', source.read_text(), re.MULTILINE)
    assert found, f'DEFAULT_FETCH_TIMEOUT_MS literal not found in {source}'
    return int(found.group(1).replace('_', '')) / 1000


def test_the_starting_budget_fits_under_the_desktop_request_deadline():
    """A write waits at most the starting budget plus one read that began just before it ran out;
    the forward still needs its own seconds before Desktop cuts the request."""
    desktop = _desktop_request_timeout_s()
    assert desktop >= 1.0  # a sanity floor on the parse, not on Desktop's choice
    assert sod.OWNER_STARTING_BUDGET_S + sod.OWNER_DISCOVERY_ATTEMPT_S < desktop
    assert sod.OWNER_DISCOVERY_BUDGET_S < sod.OWNER_STARTING_BUDGET_S
    assert 0 < sod.OWNER_STARTING_POLL_S <= 1.0


def _fast_poll(monkeypatch):
    _no_backoff(monkeypatch)
    monkeypatch.setattr(sod, 'OWNER_STARTING_POLL_S', 0.0)


@contextmanager
def _held(attempts):
    @contextmanager
    def held(homes):
        attempts.append(homes)
        raise __import__('gateway.runtime_ownership', fromlist=['x']).OwnershipConflict(DRAIN_ADVICE)
        yield
    yield held


@pytest.mark.asyncio
async def test_a_write_during_starting_sends_exactly_one_mutation_once_ready(tmp_path, monkeypatch):
    from hermes_cli import web_server_sessions

    _fast_poll(monkeypatch)
    request = _standalone_request(monkeypatch, tmp_path)
    seen = _reads(monkeypatch, [STARTING, STARTING, STARTING, STARTING, READY])
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
    assert len(seen) == 5
    assert [method for method, _ in rpcs] == ['session.mutate']
    assert rpcs[0][1]['request_id'] == 'req-1'
    assert offline == []


@pytest.mark.asyncio
async def test_a_gateway_still_starting_past_the_budget_answers_retry_not_drain(tmp_path, monkeypatch):
    """The starting gateway holds the lock: the offline write loses (never bypassed), nothing is
    forwarded, and the 503 asks for a retry instead of telling the user to stop the gateway."""
    from hermes_cli import web_server_sessions

    _fast_poll(monkeypatch)
    monkeypatch.setattr(sod, 'OWNER_STARTING_BUDGET_S', 0.3)
    request = _standalone_request(monkeypatch, tmp_path)
    seen = _reads(monkeypatch, [STARTING])
    attempts, connects = [], []
    monkeypatch.setattr('hermes_cli.gateway_client.connect_gateway', lambda **kw: connects.append(kw))
    with _held(attempts) as held:
        monkeypatch.setattr('gateway.runtime_ownership.exclusive_maintenance', held)
        with pytest.raises(HTTPException) as exc:
            await web_server_sessions._mutate_session_request(
                request, None, 's1', request_id='req-1', expected_revision=0,
                operation='sidebar', payload={'archived': True})
    assert exc.value.status_code == 503
    assert exc.value.detail == sod.STARTING_DETAIL
    assert 'Drain' not in exc.value.detail and 'stop' not in exc.value.detail
    assert len(seen) > sod.OWNER_DISCOVERY_ATTEMPTS and len(attempts) == 1 and connects == []


@pytest.mark.asyncio
async def test_a_refusal_behind_a_held_lock_keeps_the_lock_remedy(tmp_path, monkeypatch):
    """Unchanged: a verdict that is not about time is final on its first read and keeps its 503."""
    from hermes_cli import web_server_sessions

    _fast_poll(monkeypatch)
    request = _standalone_request(monkeypatch, tmp_path)
    seen = _reads(monkeypatch, [GatewayDiscovery('draining', reason_code='update_paused')])
    attempts = []
    with _held(attempts) as held:
        monkeypatch.setattr('gateway.runtime_ownership.exclusive_maintenance', held)
        with pytest.raises(HTTPException) as exc:
            await web_server_sessions._mutate_session_request(
                request, None, 's1', request_id='req-1', expected_revision=0,
                operation='sidebar', payload={'archived': True})
    assert (exc.value.status_code, len(seen), len(attempts)) == (503, 1, 1)
    assert exc.value.detail.startswith('owner_unavailable: update_paused: Exclusive maintenance refused')


@pytest.mark.asyncio
async def test_an_absent_owner_still_edits_offline_on_the_first_read(tmp_path, monkeypatch):
    from hermes_cli import web_server_sessions

    request = _standalone_request(monkeypatch, tmp_path)
    seen = _reads(monkeypatch, [GatewayDiscovery('absent'), STARTING])
    monkeypatch.setattr(web_server_sessions, '_mutate_offline_store', lambda profile, actor, params:
                        {'session_id': params['session_id'], 'operation': 'sidebar', 'revision': 1})
    result = await web_server_sessions._mutate_session_request(
        request, None, 's1', request_id='req-1', expected_revision=0,
        operation='sidebar', payload={'pinned': True})
    assert result['revision'] == 1 and len(seen) == 1


def test_live_owner_forward_answers_the_starting_detail(tmp_path, monkeypatch):
    """``_on_live_owner`` (mute, owner-backfill) shares the same discovery and detail."""
    import gateway.runtime_ownership as ownership
    import hermes_state
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli.web_routers.sessions import manage_router

    _fast_poll(monkeypatch)
    monkeypatch.setattr(sod, 'OWNER_STARTING_BUDGET_S', 0.3)
    home = tmp_path / 'state'
    home.mkdir(mode=0o700)
    seen = _reads(monkeypatch, [STARTING])

    @contextmanager
    def held(_homes, **_kw):
        raise ownership.OwnershipConflict(DRAIN_ADVICE)
        yield
    monkeypatch.setattr(ownership, 'exclusive_maintenance', held)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', home / 'state.db')
    app = FastAPI()
    app.include_router(manage_router)
    with TestClient(app) as client:
        refused = client.post('/api/sessions/owner-backfill', json={})
    assert refused.status_code == 503, refused.text
    assert refused.json()['detail'] == sod.STARTING_DETAIL
    assert len(seen) > sod.OWNER_DISCOVERY_ATTEMPTS


# --- Concurrent writes share the starting re-reads of one home. ---

N_WRITES = 20


def _counted_reads(monkeypatch, verdict_for):
    """Discovery answers ``verdict_for(n)`` for the n-th read (1-based), counted across callers."""
    import threading

    lock, seen = threading.Lock(), []

    def fake(home, timeout=2.0):
        with lock:
            seen.append(timeout)
            n = len(seen)
        return verdict_for(n)
    monkeypatch.setattr('hermes_cli.gateway_runtime.discover_gateway_endpoint', fake)
    return seen


@pytest.mark.asyncio
async def test_concurrent_writes_during_starting_share_reads_and_each_sends_one_mutation(tmp_path, monkeypatch):
    """A bulk archive sent while the gateway starts: reads do not grow with the number of writes,
    every write lands once the gateway is live, and each sends exactly one mutation."""
    import asyncio
    from hermes_cli import web_server_sessions

    _no_backoff(monkeypatch)
    monkeypatch.setattr(sod, 'OWNER_STARTING_POLL_S', 0.02)
    request = _standalone_request(monkeypatch, tmp_path)
    seen = _counted_reads(monkeypatch, lambda n: STARTING if n <= 4 else READY)
    rpcs, offline = [], []

    class Client:
        async def rpc(self, method, **params):
            rpcs.append(params['request_id'])
            return {'session_id': params['session_id'], 'operation': 'sidebar', 'revision': 1}

    @asynccontextmanager
    async def connect(endpoint=None, subject=None):
        assert endpoint == 'owner-endpoint'
        yield Client()
    monkeypatch.setattr('hermes_cli.gateway_client.connect_gateway', connect)
    monkeypatch.setattr(web_server_sessions, '_mutate_offline_store',
                        lambda profile, actor, params: offline.append(params))
    results = await asyncio.gather(*(web_server_sessions._mutate_session_request(
        request, None, f's{i}', request_id=f'req-{i}', expected_revision=0,
        operation='sidebar', payload={'archived': True}, expected_generation=0) for i in range(N_WRITES)))
    assert [result['ok'] for result in results] == [True] * N_WRITES
    assert sorted(rpcs) == sorted(f'req-{i}' for i in range(N_WRITES))  # one mutation each
    assert offline == []
    # Four starting reads, then the one that saw it live: a constant, not one poll per write.
    assert 5 <= len(seen) <= 8, len(seen)


@pytest.mark.asyncio
async def test_concurrent_writes_behind_a_persisting_start_each_answer_the_starting_503(tmp_path, monkeypatch):
    import asyncio
    from hermes_cli import web_server_sessions

    _no_backoff(monkeypatch)
    budget, poll = 0.3, 0.05
    monkeypatch.setattr(sod, 'OWNER_STARTING_BUDGET_S', budget)
    monkeypatch.setattr(sod, 'OWNER_STARTING_POLL_S', poll)
    request = _standalone_request(monkeypatch, tmp_path)
    seen = _counted_reads(monkeypatch, lambda _n: STARTING)
    attempts, connects = [], []
    monkeypatch.setattr('hermes_cli.gateway_client.connect_gateway', lambda **kw: connects.append(kw))
    with _held(attempts) as held:
        monkeypatch.setattr('gateway.runtime_ownership.exclusive_maintenance', held)
        results = await asyncio.gather(*(web_server_sessions._mutate_session_request(
            request, None, f's{i}', request_id=f'req-{i}', expected_revision=0,
            operation='sidebar', payload={'archived': True}) for i in range(N_WRITES)),
            return_exceptions=True)
    refusals = [exc for exc in results if isinstance(exc, HTTPException)]
    assert len(refusals) == N_WRITES, results
    assert {(exc.status_code, exc.detail) for exc in refusals} == {(503, sod.STARTING_DETAIL)}
    assert len(attempts) == N_WRITES and connects == []  # each tried the lock once; none forwarded
    # Reads are spaced by the poll interval across all writes, so they fit the budget's polls.
    assert sod.OWNER_DISCOVERY_ATTEMPTS < len(seen) <= budget / poll + 4, len(seen)


@pytest.mark.asyncio
async def test_a_late_caller_keeps_its_own_budget(tmp_path, monkeypatch):
    """A write that joins a starting wait already under way waits its own budget from its own first
    read: it is neither cut at the earlier write's deadline nor kept past its own."""
    import asyncio

    budget, poll = 0.6, 0.05
    monkeypatch.setattr(sod, 'OWNER_STARTING_BUDGET_S', budget)
    monkeypatch.setattr(sod, 'OWNER_STARTING_POLL_S', poll)
    loop, fourth = asyncio.get_running_loop(), asyncio.Event()

    def verdict(n):
        if n == 4:
            loop.call_soon_threadsafe(fourth.set)
        return STARTING
    seen = _counted_reads(monkeypatch, verdict)

    async def timed():
        began = time.monotonic()
        result = await sod.discover_session_owner(tmp_path)
        return result, began, time.monotonic()

    first = asyncio.ensure_future(timed())
    await fourth.wait()
    late = await timed()
    early = await first
    assert early[0] is STARTING and late[0] is STARTING
    assert late[1] > early[1]
    for _verdict, began, ended in (early, late):
        assert budget - poll - sod._MIN_ATTEMPT_S - 0.05 < ended - began <= budget + 1.0, (began, ended)
    assert late[2] > early[2]  # the late write was not cut at the first write's deadline
    assert len(seen) <= (late[2] - early[1]) / poll + 3, len(seen)


@pytest.mark.asyncio
async def test_a_shared_read_is_joined_only_within_one_poll_interval(tmp_path, monkeypatch):
    """A caller never takes a verdict that began more than one poll before it asked, nor its own
    previous read again; a first read never reuses a read that already answered."""
    from hermes_cli import gateway_runtime

    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', lambda home, timeout=2.0: STARTING)
    poll = sod.OWNER_STARTING_POLL_S
    read = sod._shared_read(tmp_path, 1.5, asked=10.0, after=None, settled_ok=False)
    assert sod._shared_read(tmp_path, 1.5, asked=10.0 + poll / 2, after=None, settled_ok=False) is read
    assert await read.verdict is STARTING
    assert sod._shared_read(tmp_path, 1.5, asked=10.0 + poll / 2, after=9.0, settled_ok=True) is read
    newer = sod._shared_read(tmp_path, 1.5, asked=10.0 + poll / 2, after=None, settled_ok=False)
    assert newer is not read and await newer.verdict is STARTING  # first reads: in flight only
    stale = sod._shared_read(tmp_path, 1.5, asked=newer.began + poll + 0.01, after=None, settled_ok=True)
    assert stale is not newer
    own = sod._shared_read(tmp_path, 1.5, asked=stale.began, after=stale.began, settled_ok=True)
    assert own is not stale
    other = sod._shared_read(tmp_path / 'other', 1.5, asked=own.began, after=None, settled_ok=True)
    assert other is not own
    for pending in (stale, own, other):
        await pending.verdict


# --- A real reservation, and a real gateway caught while it starts (never a live one). ---

BODY = {'request_id': 'archive-1', 'expected_revision': 0, 'expected_generation': 0,
        'archived': True, 'pinned': False}


def _seeded_home(tmp_path):
    from tests.gateway.test_mute_live_owner import _seed

    home = tmp_path / 'state'
    home.mkdir(mode=0o700)
    _seed(home, 'chat')
    return home


@pytest.mark.platforms('linux')
@pytest.mark.asyncio
async def test_a_real_reservation_without_a_socket_refuses_with_retry_then_the_retry_lands(tmp_path, monkeypatch):
    """The lock reserved before any PID or control socket is exactly what a starting gateway
    looks like. The write is refused in budget with nothing changed; the same request lands
    once the reservation is gone."""
    from gateway.runtime_ownership import ProfileOwnership
    from hermes_cli import gateway_runtime

    home = _seeded_home(tmp_path)
    monkeypatch.setattr(sod, 'OWNER_STARTING_BUDGET_S', 1.0)
    monkeypatch.setattr(sod, 'OWNER_STARTING_POLL_S', 0.05)
    real, reads = gateway_runtime.discover_gateway_endpoint, []

    def recorded(path, timeout=2.0):
        verdict = real(path, timeout=timeout)
        reads.append(verdict.reason_code or verdict.state)
        return verdict
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', recorded)
    app = _standalone_app(home, monkeypatch)
    starting = ProfileOwnership()
    starting.reserve([home])
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://localhost', headers=AUTH, timeout=30) as client:
            started = time.monotonic()
            refused = await client.patch('/api/sessions/chat', json=BODY)
            elapsed = time.monotonic() - started
            assert refused.status_code == 503, refused.text
            assert refused.json()['detail'] == sod.STARTING_DETAIL
            assert set(reads) == {'starting'} and len(reads) > sod.OWNER_DISCOVERY_ATTEMPTS
            assert elapsed < 1.0 + 1.5, elapsed
            assert _row(home) == (0, 0)  # the held lock was never bypassed
            starting.close()
            retried = await client.patch('/api/sessions/chat', json=BODY)
            assert retried.status_code == 200, retried.text
            assert _row(home)[0] == 1
    finally:
        starting.close()


@contextmanager
def _gateway_frozen_while_starting(tmp_path, home, discover):
    """A real gateway SIGSTOPped the moment it holds the profile but is not READY, so a write is
    certain to arrive inside the startup window. Yields (process, log); the caller resumes it."""
    from tests.child_log import ChildLog

    user = tmp_path / 'user'
    user.mkdir()
    (home / 'config.yaml').write_text(
        '{"model": {"provider": "custom", "default": "m", "base_url": "http://127.0.0.1:1/v1"},'
        ' "auxiliary": {"title_generation": {"enabled": false}}}')
    root = Path(__file__).resolve().parents[2]
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ', 'TIRITH_ENABLED') if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               PYTHONUNBUFFERED='1')
    with ChildLog(tmp_path / 'gateway.log') as log:
        process = subprocess.Popen([sys.executable, '-m', 'gateway.run'], cwd=root, env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 60
            while discover(home, timeout=1.0).state != 'starting':  # a busy poll: the window is short
                assert process.poll() is None and time.monotonic() < deadline, log.text()
            os.kill(process.pid, signal.SIGSTOP)
            yield process, log
        finally:
            if process.poll() is None:
                os.kill(process.pid, signal.SIGCONT)
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@pytest.mark.platforms('linux')
@pytest.mark.asyncio
async def test_a_write_sent_while_a_real_gateway_starts_lands_exactly_once(tmp_path, monkeypatch):
    from hermes_cli import gateway_runtime

    home = _seeded_home(tmp_path)
    real, reads = gateway_runtime.discover_gateway_endpoint, []
    with _gateway_frozen_while_starting(tmp_path, home, real) as (process, log):
        def starting_then_resumed(path, timeout=2.0):
            verdict = real(path, timeout=timeout)
            reads.append(verdict.reason_code or verdict.state)
            if len(reads) == 1:
                os.kill(process.pid, signal.SIGCONT)
            return verdict
        monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', starting_then_resumed)
        app = _standalone_app(home, monkeypatch)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://localhost', headers=AUTH, timeout=60) as client:
            done = await client.patch('/api/sessions/chat', json=BODY)
            assert done.status_code == 200, (done.text, reads, log.text())
            # Frozen before READY: the lock without a socket, or a socket that cannot answer.
            assert reads[0] in ('starting', 'control_timeout'), reads
            assert reads[-1] == 'ready' and 'absent' not in reads, reads
            assert done.json()['revision'] == 1
            assert _row(home) == (1, 1)
            again = await client.patch('/api/sessions/chat', json=BODY)
            assert again.json() == done.json()
            assert _row(home) == (1, 1)
