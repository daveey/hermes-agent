"""A ``session.create`` reply never waits on the banner inventory past its budget.

On the mini (Nov 13) one canonical ``session.create`` took 84 s from ws accept to reply: the
authority's resume awaited ``session_inventory`` (every tool ``check_fn``, the skills walk, MCP
status, git) under GIL load, past the client's 30 s RPC budget, and every retry paid it again. The
reply now carries the lazy shape once the budget passes, and the full inventory follows as a
``session.info`` event; ``--oneshot`` / ``--source tool`` replies never build it at all."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from tests.waiting import async_wait_until


async def _gateway(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection

    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text(
        'model: {default: gpt-5.4, provider: custom, base_url: "http://127.0.0.1:9/v1"}\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(run, '_hermes_home', home)
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={}, _draining=False,
                             _cached_agent_for=lambda route: None)
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = await initialize_session_authority(runner, profile_id=str(home), instance_id='first')
    connection = AuthorityConnection(authority, object(), {'user_id': 'human'})

    async def call(method, **params):
        reply = await connection.dispatch({'id': 1, 'method': method, 'params': params})
        assert 'result' in reply, reply
        return reply['result']

    return SimpleNamespace(authority=authority, call=call, cwd=str(tmp_path))


def _slow_inventory(monkeypatch, *, hold_s):
    """``session_inventory`` that takes *hold_s* (60 s: the mini's order) unless released first."""
    from gateway import session_local_inventory
    release, calls = threading.Event(), []

    def slow(authority, policy, agent, cwd):
        calls.append(policy.source if policy is not None else None)
        release.wait(hold_s)
        return {'tools': {'terminal': ['terminal']}, 'skills': {'research': ['arxiv']}, 'mcp_servers': []}

    monkeypatch.setattr(session_local_inventory, 'session_inventory', slow)
    return release, calls


def _info_frames(authority, sid):
    frames = []
    authority.sessions[sid].event_stream.observers.add(
        lambda frame: frames.append(frame['params']['payload']) if frame['params']['type'] == 'session.info' else None)
    return frames


@pytest.mark.asyncio
async def test_create_reply_does_not_wait_on_a_60s_inventory_and_info_follows(tmp_path, monkeypatch):
    gateway = await _gateway(tmp_path, monkeypatch)
    from gateway.session_local_inventory import REPLY_INVENTORY_BUDGET_S
    from tui_gateway.contracts.registry import CANONICAL_METHODS
    release, calls = _slow_inventory(monkeypatch, hold_s=60)
    try:
        started = time.monotonic()
        created = await gateway.call('session.create', request_id='slow', source='tui', cwd=gateway.cwd)
        elapsed = time.monotonic() - started
        # The budget plus room for a loaded worker (3.1 s seen at load 26), far below the 60 s build.
        assert elapsed < REPLY_INVENTORY_BUDGET_S + 10, f'session.create reply took {elapsed:.1f}s behind the inventory'
        CANONICAL_METHODS['session.create'].result.model_validate(created)
        info = created['info']
        assert info['inventory_pending'] is True and info['tools'] == {} and info['skills'] == {}, info
        assert info['lazy'] is True and info['source'] == 'tui'
        assert calls == ['tui']
        frames = _info_frames(gateway.authority, created['session_id'])
        assert frames == []  # nothing published before the build finishes
    finally:
        release.set()
    await async_wait_until(lambda: frames, what='late session.info published')
    assert len(frames) == 1, frames
    late = frames[0]
    assert late['inventory_pending'] is False
    assert late['tools'] == {'terminal': ['terminal']} and late['skills'] == {'research': ['arxiv']}
    assert late['tool_count'] == 1 and late['source'] == 'tui' and late['lazy'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['oneshot', 'tool'])
async def test_non_interactive_sources_never_build_the_inventory(tmp_path, monkeypatch, source):
    gateway = await _gateway(tmp_path, monkeypatch)
    release, calls = _slow_inventory(monkeypatch, hold_s=60)
    try:
        started = time.monotonic()
        created = await gateway.call('session.create', request_id=source, source=source, cwd=gateway.cwd)
        assert time.monotonic() - started < 3
    finally:
        release.set()
    assert calls == []
    assert created['info']['inventory_pending'] is True and created['info']['tools'] == {}


@pytest.mark.asyncio
async def test_inventory_ready_within_budget_rides_the_reply_without_an_event(tmp_path, monkeypatch):
    gateway = await _gateway(tmp_path, monkeypatch)
    release, calls = _slow_inventory(monkeypatch, hold_s=60)
    release.set()  # an unloaded gateway: the inventory is ready at once
    created = await gateway.call('session.create', request_id='fast', source='tui', cwd=gateway.cwd)
    info = created['info']
    assert 'inventory_pending' not in info and info['tools'] == {'terminal': ['terminal']}, info
    frames = _info_frames(gateway.authority, created['session_id'])
    resumed = await gateway.call('session.resume', session_id=created['session_id'])
    assert resumed['info']['tools'] == {'terminal': ['terminal']}
    await asyncio.sleep(0)
    assert frames == [] and calls == ['tui', 'tui']


@pytest.mark.asyncio
async def test_concurrent_opens_of_one_session_share_one_build(tmp_path, monkeypatch):
    gateway = await _gateway(tmp_path, monkeypatch)
    from gateway import session_local_inventory
    release, calls = _slow_inventory(monkeypatch, hold_s=60)
    monkeypatch.setattr(session_local_inventory, 'REPLY_INVENTORY_BUDGET_S', 0.05)
    try:
        created = await gateway.call('session.create', request_id='share', source='tui', cwd=gateway.cwd)
        sid = created['session_id']
        frames = _info_frames(gateway.authority, sid)
        # A second open (another viewer, or the client's retry) while the first build still runs.
        again = await gateway.call('session.resume', session_id=sid)
        assert again['info']['inventory_pending'] is True
        assert calls == ['tui']
    finally:
        release.set()
    await async_wait_until(lambda: frames, what='late session.info published')
    assert len(frames) == 1 and frames[0]['tools'] == {'terminal': ['terminal']}
    assert session_local_inventory._pending == {}


@pytest.mark.asyncio
async def test_a_shared_build_publishes_only_after_every_pending_reply_holding_it(tmp_path, monkeypatch):
    """Two opens of one session wait on the same build and both reply pending. The build finishes
    while both replies are still being assembled: releasing the first reply's hold publishes nothing
    (the second reply's pending shape would overwrite the inventory), the second publishes it once."""
    gateway = await _gateway(tmp_path, monkeypatch)
    from gateway import session_controls, session_local_inventory
    release, calls = _slow_inventory(monkeypatch, hold_s=60)
    monkeypatch.setattr(session_local_inventory, 'REPLY_INVENTORY_BUDGET_S', 0.05)
    try:
        created = await gateway.call('session.create', request_id='holds', source='tui', cwd=gateway.cwd)
        sid = created['session_id']
        assert created['info']['inventory_pending'] is True
        order = []
        gateway.authority.sessions[sid].event_stream.observers.add(
            lambda frame: order.append(frame['params']['payload']) if frame['params']['type'] == 'session.info' else None)
        gates, real_transcript = [], session_controls._resume_transcript

        async def gated_transcript(*args):
            gate = asyncio.Event()
            gates.append(gate)
            await gate.wait()  # this reply is still being assembled
            return await real_transcript(*args)

        monkeypatch.setattr(session_controls, '_resume_transcript', gated_transcript)

        async def resume(label):
            reply = await gateway.call('session.resume', session_id=sid)
            order.append(label)
            return reply

        first, second = (asyncio.ensure_future(resume(label)) for label in ('first reply', 'second reply'))
        await async_wait_until(lambda: len(gates) == 2, what='both replies past reply_info')
        (entry,) = session_local_inventory._pending.values()
        assert entry.holds == 2 and calls == ['tui']
    finally:
        release.set()
    await async_wait_until(lambda: entry.task.done(), what='shared inventory built')
    for _ in range(5):
        await asyncio.sleep(0)  # the build's done callback (_settle) has run
    assert order == [], order  # both replies still hold it
    gates[0].set()
    await asyncio.wait({first, second}, return_when=asyncio.FIRST_COMPLETED)
    for _ in range(5):
        await asyncio.sleep(0)  # the first reply's hold is released (call_soon)
    assert entry.holds == 1 and len(order) == 1 and isinstance(order[0], str), order
    gates[1].set()
    replies = await asyncio.gather(first, second)
    assert all(reply['info']['inventory_pending'] is True for reply in replies)
    await async_wait_until(lambda: len(order) == 3, what='late session.info published')
    for _ in range(5):
        await asyncio.sleep(0)
    assert sorted(order[:2]) == ['first reply', 'second reply'] and len(order) == 3, order
    assert order[2]['inventory_pending'] is False and order[2]['tools'] == {'terminal': ['terminal']}
    assert session_local_inventory._pending == {}


def _failing_inventory(monkeypatch):
    """``session_inventory`` that raises once released (a builder bug, a closed profile)."""
    from gateway import session_local_inventory
    release = threading.Event()

    def failing(authority, policy, agent, cwd):
        release.wait(60)
        raise RuntimeError('inventory builder crashed')

    monkeypatch.setattr(session_local_inventory, 'session_inventory', failing)
    return release


@pytest.mark.asyncio
async def test_late_inventory_reaches_the_viewer_only_after_the_reply(tmp_path, monkeypatch):
    """The build can finish while the pending reply is still being assembled (a long chat's
    transcript read): its ``session.info`` must follow the reply, never precede it. Before it, Ink
    drops it on create (the session id is not known yet) and a resume reply's pending shape
    overwrites the merged inventory, so the banner stays at "…"."""
    gateway = await _gateway(tmp_path, monkeypatch)
    from gateway import session_controls, session_local_inventory
    release, calls = _slow_inventory(monkeypatch, hold_s=60)
    monkeypatch.setattr(session_local_inventory, 'REPLY_INVENTORY_BUDGET_S', 0.05)
    order, real_transcript = [], session_controls._resume_transcript

    async def transcript_after_the_build(authority, ref, *rest):
        authority.sessions[ref.session_id].event_stream.observers.add(
            lambda frame: order.append(frame['params']['payload']) if frame['params']['type'] == 'session.info' else None)
        release.set()
        await async_wait_until(lambda: calls and all(entry.task.done() for entry in session_local_inventory._pending.values()),
                               what='inventory built')
        for _ in range(5):
            await asyncio.sleep(0)  # the build's done callbacks run before the reply is returned
        return await real_transcript(authority, ref, *rest)

    monkeypatch.setattr(session_controls, '_resume_transcript', transcript_after_the_build)
    created = await gateway.call('session.create', request_id='race', source='tui', cwd=gateway.cwd)
    order.append('reply')
    assert created['info']['inventory_pending'] is True
    await async_wait_until(lambda: len(order) == 2, what='late session.info published')
    assert order[0] == 'reply', order
    assert order[1]['inventory_pending'] is False and order[1]['tools'] == {'terminal': ['terminal']}
    assert session_local_inventory._pending == {}


@pytest.mark.asyncio
async def test_a_build_failing_after_the_budget_publishes_a_settled_empty_inventory(tmp_path, monkeypatch):
    gateway = await _gateway(tmp_path, monkeypatch)
    from gateway import session_local_inventory
    monkeypatch.setattr(session_local_inventory, 'REPLY_INVENTORY_BUDGET_S', 0.05)
    release = _failing_inventory(monkeypatch)
    try:
        created = await gateway.call('session.create', request_id='fails-late', source='tui', cwd=gateway.cwd)
        assert created['info']['inventory_pending'] is True
        frames = _info_frames(gateway.authority, created['session_id'])
    finally:
        release.set()
    await async_wait_until(lambda: frames, what='terminal session.info published')
    assert len(frames) == 1, frames
    assert frames[0]['inventory_pending'] is False, frames[0]
    assert frames[0]['tools'] == {} and frames[0]['skills'] == {} and frames[0]['mcp_servers'] == []
    assert frames[0]['tool_count'] == 0 and frames[0]['source'] == 'tui'
    assert session_local_inventory._pending == {}


@pytest.mark.asyncio
async def test_a_build_failing_inside_the_budget_answers_settled_not_pending(tmp_path, monkeypatch):
    gateway = await _gateway(tmp_path, monkeypatch)
    from gateway import session_local_inventory
    _failing_inventory(monkeypatch).set()  # fails at once
    created = await gateway.call('session.create', request_id='fails-fast', source='tui', cwd=gateway.cwd)
    info = created['info']
    assert info['inventory_pending'] is False and info['tools'] == {} and info['skills'] == {}, info
    frames = _info_frames(gateway.authority, created['session_id'])
    for _ in range(5):
        await asyncio.sleep(0)
    assert frames == [] and session_local_inventory._pending == {}
