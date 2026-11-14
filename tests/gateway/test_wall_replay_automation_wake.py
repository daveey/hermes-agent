"""An automated wake a held quota wall refused before it ran is redelivered once after the reset.

Hub chat local-be7f1ded… (2026-11-14): three Kanban notifier wakes were committed to the chat's FIFO while
its pinned anthropic/claude-opus-5-5 wall had under 6 h left. A wall that short holds the pin
(``model_routing_walls.may_reroute`` is False), so each turn was refused before the agent was built with
``held_reply`` ("resend after the reset"). The notifier took the FIFO receipt as final and nobody resent
an automated notice: it was lost until another event woke the chat.

These run the real gateway runner, authority, FIFO and resolution failure path; only the runtime
resolver is replaced, by one that raises the real ``SubscriptionQuotaUnavailable`` (held) or resolves.
"""
import asyncio
import json
import time

import pytest

from agent.model_routing_unavailable import SubscriptionQuotaUnavailable


def _held_wall(hours=2.0):
    wall = SubscriptionQuotaUnavailable('anthropic', 'claude-opus-5-5', time.time() + hours * 3600)
    from agent.model_routing_walls import may_reroute
    assert may_reroute(wall, None, 'explicit') is False  # a wall this short holds the pin: no reroute
    return wall


async def _authority(tmp_path, monkeypatch, instance='wall-replay'):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from hermes_state import SessionDB
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    db = SessionDB(tmp_path / 'state.db')
    runner.session_store._db = runner._session_db = db
    # A served authority's id is its profile home (``run_runtime``), the owner of its sessions' terminal
    # policy: a replay admitted inside that home's scope (``_admit``) must find its own policy there.
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id=instance, db=db)
    return runner, db, authority


def _intents(db):
    from gateway.session_wall_replay import PREFIX
    with db._read_ctx() as conn:
        return {k[len(PREFIX):]: json.loads(v) for k, v in
                conn.execute('SELECT key, value FROM state_meta WHERE key LIKE ?', (PREFIX + '%',)).fetchall()}


def _rows(db, sid):
    from hermes_state_runtime import list_session_admissions
    return list_session_admissions(db, session_id=sid, pending_only=False)


async def _settled(db, sid, count):
    from tests.waiting import async_wait_until
    await async_wait_until(lambda: len(_rows(db, sid)) >= count
                           and all(r['status'] == 'terminal' for r in _rows(db, sid)),
                           timeout=20, what=f'{count} admission(s) settled')
    return _rows(db, sid)


async def _stop(authority, db):
    tasks = [live.task for live in authority.sessions.values() if live.task]
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for handle in getattr(authority, '_wall_replay_timers', {}).values():
        handle.cancel()
    db.close()


async def _local_session(authority, tmp_path):
    from gateway.session_contract import SessionActor
    from gateway.session_local import create_local_session
    viewer = SessionActor('test-owner', authority.profile_id, frozenset(
        {'session:create', 'session:submit', 'session:read'}), 'viewer')
    ref = create_local_session(authority, viewer, dict(request_id='cli', source='tui', model='fixture',
                                                      cwd=str(tmp_path), toolsets=[]))
    await authority.attach(viewer, ref, history=False)
    return viewer, ref


def _wall_gate(runner, monkeypatch, walls):
    """The real turn path up to runtime resolution; resolution raises the head of ``walls`` (a held
    wall) or, once ``walls`` is empty, the turn 'runs' and records what it saw."""
    from gateway.session_results import execution_result
    seen = []

    def resolve(**_kwargs):
        if walls:
            raise walls[0]
        raise AssertionError('a lifted wall is answered by the stub turn below')

    monkeypatch.setattr(runner, '_resolve_session_agent_runtime', resolve)
    original = runner._handle_message_with_agent

    async def turn(event, source, quick_key, run_generation):
        seen.append(event)
        if walls:
            return await original(event, source, quick_key, run_generation)
        execution_result.get()['result'] = {'final_response': 'handled', 'messages': []}
        return 'handled'

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    return seen


@pytest.mark.asyncio
async def test_refused_kanban_wake_is_redelivered_once_after_the_reset(tmp_path, monkeypatch):
    from gateway import session_wall_replay as replay
    from gateway.session_automation import admit_route_wake
    runner, db, authority = await _authority(tmp_path, monkeypatch)
    wall = _held_wall()
    walls = [wall]
    seen = _wall_gate(runner, monkeypatch, walls)
    try:
        _viewer, ref = await _local_session(authority, tmp_path)
        live = authority.sessions[ref.session_id]
        adapter = runner._adapter_for_source(live.source)
        notice = 'kanban: task t_01a1d9725dc575c5 ready with no worker (chat_wait_unclaimed)'
        assert await admit_route_wake(runner, adapter, live.source, notice, 'kanban-wake') is True
        [refused] = await _settled(db, ref.session_id, 1)
        assert refused['outcome'] == 'failed'
        # The refusal ran the real pre-build path: the held reply, nothing reached a provider.
        assert len(seen) == 1
        intents = _intents(db)
        assert list(intents) == [refused['admission_id']]
        intent = intents[refused['admission_id']]
        assert intent['due'] == pytest.approx(wall.resets_at + replay.SLACK_S)
        assert intent['producer'] == 'kanban-wake' and intent['text'] == notice
        assert refused['admission_id'] in authority._wall_replay_timers  # armed in this process
        from gateway.session_results import admission_result
        stored = admission_result(db, refused['admission_id'])['result']
        assert 'subscription quota is out' in stored['final_response']
        assert 'deliver it once more' in stored['final_response']
        assert stored['failure_resets_at'] == pytest.approx(wall.resets_at)
        assert 'deliver it once more' in stored['error_surface']['message']  # the card says it too

        # The reset passes: the planned timer fires (driven directly; it is armed hours out).
        walls.clear()
        assert await replay.fire_replay(authority, refused['admission_id']) == 'admitted'
        rows = await _settled(db, ref.session_id, 2)
        redelivered = rows[1]
        assert redelivered['outcome'] == 'completed'
        assert json.loads(redelivered['request_id'])[2] == replay.REPLAY_TAG + json.loads(refused['request_id'])[2]
        assert seen[-1].text == notice and seen[-1].internal
        assert _intents(db) == {}
        # Once only: a late second timer finds nothing, and the deterministic identity is an exact replay.
        assert await replay.fire_replay(authority, refused['admission_id']) == 'gone'
        tail = json.loads(refused['request_id'])[2]
        assert await admit_route_wake(runner, adapter, live.source, notice, 'kanban-wake', replay_of=tail) is True
        await asyncio.sleep(0)
        assert len(_rows(db, ref.session_id)) == 2
    finally:
        await _stop(authority, db)


@pytest.mark.asyncio
async def test_a_redelivery_refused_again_gives_up(tmp_path, monkeypatch):
    from gateway import session_wall_replay as replay
    from gateway.session_automation import admit_route_wake
    runner, db, authority = await _authority(tmp_path, monkeypatch)
    _wall_gate(runner, monkeypatch, [_held_wall()])
    try:
        _viewer, ref = await _local_session(authority, tmp_path)
        live = authority.sessions[ref.session_id]
        adapter = runner._adapter_for_source(live.source)
        assert await admit_route_wake(runner, adapter, live.source, 'kanban: t_1 blocked', 'kanban-wake') is True
        [refused] = await _settled(db, ref.session_id, 1)
        # The session's turn task has exited, so the redelivery starts a new one inside _admit's profile
        # scope (as a timer-fired replay does in production); before that, CI raced with this test.
        from tests.waiting import async_wait_until
        await async_wait_until(lambda: live.task is None or live.task.done(), timeout=20, what='the session idle')
        # The wall was renewed past its reset: the one redelivery is refused too.
        assert await replay.fire_replay(authority, refused['admission_id']) == 'admitted'
        rows = await _settled(db, ref.session_id, 2)
        assert rows[1]['outcome'] == 'failed'
        assert _intents(db) == {}  # nothing more is scheduled
        from gateway.session_results import admission_result
        assert replay.GAVE_UP_LINE in admission_result(db, rows[1]['admission_id'])['result']['final_response']
    finally:
        await _stop(authority, db)


@pytest.mark.asyncio
async def test_a_person_s_message_keeps_the_held_reply_and_schedules_nothing(tmp_path, monkeypatch):
    from agent.model_routing_walls import held_reply
    from gateway.session_contract import Submission
    runner, db, authority = await _authority(tmp_path, monkeypatch)
    wall = _held_wall()
    _wall_gate(runner, monkeypatch, [wall])
    try:
        viewer, ref = await _local_session(authority, tmp_path)
        await authority.submit(viewer, Submission('human-1', ref, {'text': 'please check the board'}, 'queue'))
        [row] = await _settled(db, ref.session_id, 1)
        assert row['outcome'] == 'failed'
        assert _intents(db) == {}
        from gateway.session_results import admission_result
        assert admission_result(db, row['admission_id'])['result']['final_response'] == held_reply(wall)
    finally:
        await _stop(authority, db)


@pytest.mark.asyncio
async def test_a_planned_redelivery_survives_a_restart(tmp_path, monkeypatch):
    """The plan is durable: a fresh authority on the same store re-arms it at boot."""
    from gateway import session_wall_replay as replay
    from gateway.session_automation import admit_route_wake
    runner, db, authority = await _authority(tmp_path, monkeypatch)
    _wall_gate(runner, monkeypatch, [_held_wall()])
    try:
        _viewer, ref = await _local_session(authority, tmp_path)
        live = authority.sessions[ref.session_id]
        adapter = runner._adapter_for_source(live.source)
        assert await admit_route_wake(runner, adapter, live.source, 'kanban: t_1 blocked', 'kanban-wake') is True
        [refused] = await _settled(db, ref.session_id, 1)
        assert list(_intents(db)) == [refused['admission_id']]
    finally:
        await _stop(authority, db)
    _runner2, db2, authority2 = await _authority(tmp_path, monkeypatch, instance='wall-replay-2')
    try:
        assert refused['admission_id'] in authority2._wall_replay_timers
        handle = authority2._wall_replay_timers[refused['admission_id']]
        assert handle.when() - asyncio.get_running_loop().time() >= replay.MIN_DELAY_S - 1
    finally:
        await _stop(authority2, db2)


@pytest.mark.parametrize('request_id,principal,result,planned', [
    (['kanban-wake', 'r', 'x'], 'automation:r', {'failure_reason': 'route_refused'}, True),
    (['background-wake', 'r', 'x'], 'automation:r', {'failure_reason': 'auth_permanent'}, True),
    (['kanban-wake', 'r', 'wall-replay:x'], 'automation:r', {'failure_reason': 'route_refused'}, False),
    (['loop-tick', 'r', 'x'], 'automation:r', {'failure_reason': 'route_refused'}, False),
    (['kanban-wake', 'r', 'x'], 'automation:other', {'failure_reason': 'route_refused'}, False),
    (['kanban-wake', 'r', 'x'], 'automation:r', {'failure_reason': 'rate_limit'}, False),
    (['kanban-wake', 'r', 'x'], 'automation:r', {'failure_reason': 'route_refused', 'api_calls': 1}, False),
    (['kanban-wake', 'r', 'x'], 'automation:r', {'failure_reason': 'route_refused', 'failed': False}, False),
    (['kanban-wake', 'r', 'x'], 'automation:r', {'failure_reason': 'route_refused', 'failure_resets_at': None}, False),
    ('human-1', 'viewer:local', {'failure_reason': 'route_refused'}, False),
], ids=['kanban', 'background', 'replay', 'loop-tick', 'foreign-route', 'rate-limit', 'ran', 'not-failed',
        'no-reset', 'human'])
def test_only_a_pre_build_wall_refusal_of_an_automated_wake_plans_one(request_id, principal, result, planned):
    from gateway.session_wall_replay import MAX_DELAY_S, plan_due
    now = 1_900_000_000.0
    row = {'request_id': request_id if isinstance(request_id, str) else json.dumps(request_id),
           'principal_id': principal}
    full = {'failed': True, 'api_calls': 0, 'failure_resets_at': now + 3600, **result}
    assert (plan_due(row, full, now=now) is not None) is planned
    if planned:
        assert plan_due(row, {**full, 'failure_resets_at': now + MAX_DELAY_S + 1}, now=now) is None
        assert plan_due(row, {**full, 'failure_resets_at': now - 10}, now=now) == now + 60


def test_resolution_failure_result_carries_the_wall_reset():
    from gateway.config import Platform
    from gateway.run_turn_runner import _resolution_failure_result
    wall = SubscriptionQuotaUnavailable('anthropic', 'claude-opus-5-5', 1794665999.37)
    for platform in (Platform.LOCAL, Platform.TELEGRAM):
        result = _resolution_failure_result(wall, platform)
        assert result['failure_resets_at'] == pytest.approx(1794665999.37)
        assert result['api_calls'] == 0
    assert 'failure_resets_at' not in _resolution_failure_result(RuntimeError('no creds'), Platform.LOCAL)


@pytest.mark.asyncio
async def test_a_redelivery_never_folds_into_a_waiting_kanban_wake(tmp_path, monkeypatch):
    """A waiting Kanban wake keeps its own row (and its own redelivery if refused); the replay is its own."""
    from gateway import kanban_wake_fold
    from gateway.session_automation import admit_route_wake
    runner, db, authority = await _authority(tmp_path, monkeypatch)
    _wall_gate(runner, monkeypatch, [])
    admitted = []
    try:
        _viewer, ref = await _local_session(authority, tmp_path)
        live = authority.sessions[ref.session_id]
        adapter = runner._adapter_for_source(live.source)

        def no_fold(_runner):
            raise AssertionError('a redelivery entered the Kanban wake fold')

        async def admit(_adapter, event, identity):
            admitted.append(json.loads(identity))

        monkeypatch.setattr(kanban_wake_fold, 'wake_lock', no_fold)
        monkeypatch.setattr(authority, 'admit_automation', admit)
        metadata = {'kanban_cards': [{'task_id': 't_1', 'board': 'default'}]}
        assert await admit_route_wake(runner, adapter, live.source, 'kanban: t_1 blocked', 'kanban-wake',
                                      metadata, replay_of='abc') is True
        assert [parts[2] for parts in admitted] == ['wall-replay:abc']
    finally:
        await _stop(authority, db)


def _seed(db, admission_id='adm-1'):
    from gateway import session_wall_replay as replay
    intent = {'admission_id': admission_id, 'producer': 'kanban-wake', 'route': 'r', 'tail': 'x',
              'due': time.time(), 'text': 'kanban: t_1 blocked', 'metadata': {},
              'source': {'platform': 'local', 'chat_id': 'local-x', 'chat_type': 'dm'}}
    replay._write(db, replay.PREFIX + admission_id, json.dumps(intent))
    return intent


@pytest.mark.asyncio
@pytest.mark.parametrize('case,verdict,kept', [
    ('admitted', 'admitted', False),
    ('no-adapter', 'retry', True),        # not connected yet (boot): looks again, plan kept
    ('draining', 'retry', True),          # a transient store refusal: looks again
    ('refused', 'dropped', False),        # a permanent refusal: dropped, never retried
    ('no-route', 'dropped', False),       # the route takes no wakes any more
    ('exhausted', 'dropped', False),      # the retry budget is spent
    ('crash', 'retry', True),             # an unexpected admission error: looks again, within the budget
    ('retired', 'gone', True),            # a retired authority leaves it to the store's next owner
])
async def test_fire_replay_outcomes(tmp_path, monkeypatch, case, verdict, kept):
    from gateway import session_automation, session_wall_replay as replay
    from hermes_state_runtime import RuntimeStoreError
    runner, db, authority = await _authority(tmp_path, monkeypatch)
    calls = []
    try:
        intent = _seed(db)

        async def admit(_runner, _adapter, source, text, producer, metadata, replay_of=None):
            calls.append((source.chat_id, text, producer, replay_of))
            if case == 'draining':
                raise RuntimeStoreError('runtime_draining')
            if case == 'refused':
                raise RuntimeStoreError('permission_denied')
            if case == 'crash':
                raise ValueError('adapter blew up')
            return case != 'no-route'

        monkeypatch.setattr(session_automation, 'admit_route_wake', admit)
        monkeypatch.setattr(runner, '_adapter_for_source', lambda source: None if case == 'no-adapter' else object())
        if case == 'retired':
            authority.retiring = True
        tries = replay.RETRIES_MAX if case == 'exhausted' else 0
        if case == 'exhausted':
            monkeypatch.setattr(runner, '_adapter_for_source', lambda source: None)
        assert await replay.fire_replay(authority, intent['admission_id'], tries) == verdict
        assert (intent['admission_id'] in _intents(db)) is kept
        assert (intent['admission_id'] in getattr(authority, '_wall_replay_timers', {})) is (verdict == 'retry')
        if case == 'admitted':
            assert calls == [('local-x', 'kanban: t_1 blocked', 'kanban-wake', 'x')]
        if case in ('retired', 'no-adapter', 'exhausted'):
            assert calls == []
    finally:
        authority.retiring = False
        await _stop(authority, db)


@pytest.mark.asyncio
async def test_a_stale_planned_redelivery_is_dropped_at_boot_not_delivered(tmp_path, monkeypatch):
    """A plan whose time passed more than ``STALE_S`` ago (a long outage) is dropped at boot, not re-armed:
    the notice would arrive long after it mattered. A plan still in date is re-armed as before."""
    from gateway import session_automation, session_wall_replay as replay
    runner, db, authority = await _authority(tmp_path, monkeypatch)
    calls = []

    async def admit(*args, **kwargs):
        calls.append(args)
        return True

    monkeypatch.setattr(session_automation, 'admit_route_wake', admit)
    monkeypatch.setattr(runner, '_adapter_for_source', lambda source: object())
    try:
        stale = _seed(db, 'adm-stale')
        stale['due'] = time.time() - replay.MAX_DELAY_S - 60  # the gateway was down a week through it
        replay._write(db, replay.PREFIX + 'adm-stale', json.dumps(stale))
        _seed(db, 'adm-fresh')
        assert await replay.arm_pending(authority) == 1
        assert set(_intents(db)) == {'adm-fresh'}
        assert 'adm-stale' not in authority._wall_replay_timers
        assert 'adm-fresh' in authority._wall_replay_timers
        # A timer an earlier process armed for it finds nothing left to deliver.
        assert await replay.fire_replay(authority, 'adm-stale') == 'gone'
        # And a timer that fires that late (a suspended process) drops it rather than delivering it; the
        # bound is STALE_S (a day), well inside MAX_DELAY_S.
        assert replay.STALE_S < replay.MAX_DELAY_S
        stale['due'] = time.time() - replay.STALE_S - 60
        replay._write(db, replay.PREFIX + 'adm-stale', json.dumps(stale))
        assert await replay.fire_replay(authority, 'adm-stale') == 'dropped'
        assert set(_intents(db)) == {'adm-fresh'}
        assert calls == []
    finally:
        await _stop(authority, db)


@pytest.mark.asyncio
async def test_a_redelivery_whose_adapter_never_returns_is_dropped_across_restarts(tmp_path, monkeypatch):
    """The retry budget is persisted with the plan, so restarts do not reset it: a platform removed from
    config (no adapter, ever) is dropped once the budget is spent, however often the gateway restarts."""
    from gateway import session_wall_replay as replay
    _runner, db, authority = await _authority(tmp_path, monkeypatch)
    _seed(db, 'adm-orphan')
    await _stop(authority, db)
    verdicts, stored, boot = [], {}, -1
    for boot in range(replay.RETRIES_MAX + 3):
        runner, db, authority = await _authority(tmp_path, monkeypatch, instance=f'boot-{boot}')
        monkeypatch.setattr(runner, '_adapter_for_source', lambda source: None)
        try:
            stored = _intents(db)
            if 'adm-orphan' not in getattr(authority, '_wall_replay_timers', {}):
                break  # this boot found the plan spent, dropped it and armed nothing
            # The timer this process armed fires once and finds no adapter; then the gateway restarts.
            verdicts.append(await replay.fire_replay(authority, 'adm-orphan'))
        finally:
            await _stop(authority, db)
    assert 'adm-orphan' not in stored  # read by the last boot, after it dropped the plan
    # Ten looks in all (one per process here), then the next boot drops the plan instead of arming it.
    assert verdicts == ['retry'] * replay.RETRIES_MAX
    assert boot == replay.RETRIES_MAX
