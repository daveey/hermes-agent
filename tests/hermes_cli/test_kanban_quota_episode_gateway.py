"""A quota wall is a fact about one gateway's OAuth pool, not the fleet's.

2026-11-12 (t_01a1cf8d39b99b11): worker-6 and worker-2 recorded hinted Anthropic walls until 11-13..11-17
from their own pools. Every other worker then held those cards on ``subscription_quota_reset`` although
worker-5's pool had three accounts with Opus room (it was running Opus at the time). A wall recorded on
another gateway, or one of this gateway's own older than ``LOCAL_WALL_TRUST_SECONDS``, holds only while
this gateway's pool agrees; a wall this gateway just saw still holds as recorded.
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_provider_walls as walls
from hermes_cli import kanban_quota_episode as quota
from tests.hermes_cli.test_kanban_quota_episode import board as board, routing  # noqa: PLC0414 -- the self-alias is load-bearing: it suppresses F811 for the pytest fixture parameter shadowing this import

DAY = 86_400


def _clock(monkeypatch, start=1_800_000_000):
    clock = [start]
    monkeypatch.setattr(kb.time, 'time', lambda: clock[0])
    monkeypatch.setattr(walls.time, 'time', lambda: clock[0])
    return clock


def _reap_wall(conn, tid, reset, *, route_model='class:deep', route_provider=None, fields=None):
    """Real class admission/allocation, followed by a simulated exact-run quota failure."""
    from agent.model_routing import resolve_model_route
    from agent.model_routing_agent import class_requirements
    from hermes_cli.kanban_model_assignment import _assignee_config
    from hermes_cli.kanban_model_tier import apply_model_tier
    task = kb.claim_task(conn, tid, claimer=kb._claimer_id())
    assert task is not None
    pid = 71001
    conn.execute('UPDATE tasks SET worker_pid=? WHERE id=?', (pid, tid))
    apply_model_tier(conn, task, lane='ready')
    admitted = [e.payload for e in kb.list_events(conn, tid) if e.kind == 'run_route'][-1]
    assert (admitted['model'], admitted['provider'], admitted['source']) == (route_model, route_provider, 'class')
    config = _assignee_config(task.assignee)
    allocated = resolve_model_route(task.model_override, task.provider_override, config=config,
                                   session_id=f'quota-fixture-{tid}-{task.current_run_id}',
                                   requirements=class_requirements(config))
    assert (allocated['provider'], allocated['model']) in {
        (r['provider'], r['model']) for r in config['model_routing']['routes'][route_model.removeprefix('class:')]}
    receipt = {'failure_reason': 'rate_limit', 'failure_retryable': True, 'failure_resets_at': float(reset),
               'route_provider': allocated['provider'], 'route_model': allocated['model']}
    if fields is not None:
        receipt = fields
    kb._append_event(conn, tid, 'worker_result', {
        'pid': pid, 'claim_lock': task.claim_lock, 'exit_code': 75,
        'last_output': f'SubscriptionQuotaUnavailable: anthropic/claude-opus-5-5; resets at {reset}.0', **receipt,
    }, run_id=task.current_run_id)
    conn.commit()
    dispatch._record_worker_exit(pid, 75 << 8)
    dispatch.detect_crashed_workers(conn)
    assert kb.get_task(conn, tid).status == 'ready'
    with kb.write_txn(conn):
        quota.record_episode(conn, tid, task.current_run_id)
    return task.current_run_id


def _on_gateway(monkeypatch, name):
    """View the same board from another host: its claim prefix decides which recorded runs are its own."""
    monkeypatch.setattr(kb, '_host_prefix', lambda: f'{name}:')


def _pool(monkeypatch, room):
    seen = []

    def live_quota_room(provider, model):
        seen.append((provider, model))
        return (provider, model) in room
    monkeypatch.setattr(walls, 'live_quota_room', live_quota_room)
    monkeypatch.setattr(walls, 'active_walls', lambda **_k: {})
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    return seen


def _deep_routing(monkeypatch):
    config, _ = routing(monkeypatch)
    route = dict(config['model_routing']['routes']['standard'][0])
    config['model_routing']['routes']['deep'] = [
        {**route, 'provider': 'openai-codex', 'model': 'gpt-6-astra'},
        {**route, 'provider': 'anthropic', 'model': 'claude-opus-5-5'}]
    config['providers']['openai-codex'] = {'enabled': False}  # only Anthropic can serve deep here
    return config


def test_wall_recorded_on_gateway_a_admits_on_b_with_room_and_still_holds_on_a(
        board, monkeypatch, all_assignees_spawnable):
    clock = _clock(monkeypatch)
    routing(monkeypatch)
    seen = _pool(monkeypatch, {('anthropic', 'claude-opus-5-5')})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='opus card walled on A', assignee='a')
        conn.execute("UPDATE tasks SET model_override='class:deep', provider_override='auto' WHERE id=?",
                     (tid,))
        conn.commit()
        _reap_wall(conn, tid, clock[0] + 5 * DAY)
        clock[0] += 301
        # A just saw its own pool walled: it waits, whatever its pool reads now.
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        assert seen == []
        _on_gateway(monkeypatch, 'worker-b')
        assert dispatch.check_respawn_guard(conn, tid) is None
        assert set(seen) == {('anthropic', 'claude-opus-5-5')}
        spawned = []
        dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: spawned.append(
            (task.id, task.model_override, task.provider_override)))
        assert spawned == [(tid, 'class:deep', 'auto')]
        assert kb.get_task(conn, tid).model_override == 'class:deep'
        assert quota.episode(conn, tid)['providers'] == ['anthropic']


def test_wall_from_gateway_a_still_holds_on_b_when_b_pool_has_no_room(board, monkeypatch, all_assignees_spawnable):
    clock = _clock(monkeypatch)
    routing(monkeypatch)
    _pool(monkeypatch, set())  # B's pool is walled too, or empty: no positive room
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='walled everywhere', assignee='a')
        conn.execute("UPDATE tasks SET model_override='class:deep', provider_override='auto' WHERE id=?",
                     (tid,))
        conn.commit()
        _reap_wall(conn, tid, clock[0] + 5 * DAY)
        _on_gateway(monkeypatch, 'worker-b')
        clock[0] += 2 * DAY
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        spawned = []
        dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: spawned.append(task.id))
        assert spawned == []


def test_own_wall_is_rechecked_against_own_pool_only_after_the_trust_window(board, monkeypatch):
    clock = _clock(monkeypatch)
    routing(monkeypatch)
    room = {('anthropic', 'claude-opus-5-5')}
    _pool(monkeypatch, room)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='own wall', assignee='a')
        conn.execute("UPDATE tasks SET model_override='class:deep', provider_override='auto' WHERE id=?",
                     (tid,))
        conn.commit()
        _reap_wall(conn, tid, clock[0] + 5 * DAY)
        clock[0] += quota.LOCAL_WALL_TRUST_SECONDS - 1
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        clock[0] += 2
        assert dispatch.check_respawn_guard(conn, tid) is None
        room.clear()  # this pool still carries the cooldown: the wall stands
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'


@pytest.mark.parametrize('room, held', [
    ({('anthropic', 'claude-opus-5-5')}, False),   # the concrete Opus route deep allocates here has room
    ({('anthropic', 'class:deep')}, True),         # a class selector is never a pool model
    (set(), True),
])
def test_class_deep_wall_checks_the_concrete_route_the_class_allocates_here(
        board, monkeypatch, all_assignees_spawnable, room, held):
    clock = _clock(monkeypatch)
    _deep_routing(monkeypatch)
    seen = _pool(monkeypatch, room)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='class deep card', assignee='a')
        conn.execute("UPDATE tasks SET model_override='class:deep', provider_override='auto' WHERE id=?", (tid,))
        conn.commit()
        _reap_wall(conn, tid, clock[0] + 5 * DAY, route_model='class:deep', route_provider=None)
        state = quota.episode(conn, tid)
        assert state['providers'] == ['anthropic'] and state['model'] == 'class:deep'
        _on_gateway(monkeypatch, 'worker-b')
        clock[0] += 301
        assert dispatch.check_respawn_guard(conn, tid) == (('subscription_quota_reset') if held else None)
        assert ('anthropic', 'claude-opus-5-5') in seen


def test_wall_with_unknown_provider_holds_on_every_gateway(board, monkeypatch):
    clock = _clock(monkeypatch)
    routing(monkeypatch)
    seen = _pool(monkeypatch, {('anthropic', 'claude-opus-5-5')})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='unknown wall', assignee='a')
        conn.execute("UPDATE tasks SET model_override='class:standard', provider_override='auto' WHERE id=?", (tid,))
        conn.commit()
        _reap_wall(conn, tid, clock[0] + 5 * DAY, route_model='class:standard', route_provider=None,
                   fields={'failure_reason': 'rate_limit', 'failure_resets_at': float(clock[0] + 5 * DAY)})
        assert quota.episode(conn, tid)['providers'] == []
        _on_gateway(monkeypatch, 'worker-b')
        clock[0] += DAY
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        assert seen == []


def test_live_quota_room_needs_a_live_entry(monkeypatch):
    """An empty or unreadable pool is no room (``live_quota_wall() is None`` would read it as free)."""
    from agent import credential_pool

    class Pool:
        provider = 'anthropic'

        def entries(self):
            return []
    monkeypatch.setattr(credential_pool, 'load_pool', lambda *_a, **_k: Pool())
    assert walls.live_quota_wall('anthropic', 'claude-opus-5-5') is None
    assert walls.live_quota_room('anthropic', 'claude-opus-5-5') is False
    assert walls.live_quota_room('anthropic', 'class:deep') is False

    def boom(*_a, **_k):
        raise OSError('unreadable')
    monkeypatch.setattr(credential_pool, 'load_pool', boom)
    assert walls.live_quota_room('anthropic', 'claude-opus-5-5') is False


@pytest.mark.parametrize('evidence', ['provider_file', 'live_pool'])
def test_lifted_foreign_wall_still_honours_base_route_walled_elsewhere(
        board, monkeypatch, all_assignees_spawnable, evidence):
    """Review of PR #1400: lifting every wall must not skip the walled-elsewhere check on the base route.

    A card moved off codex onto Opus and walled there on A must not be admitted on B (Opus room) onto its
    codex base route while codex is walled by the provider file or B's live pool."""
    from hermes_cli.kanban_quota_episode import episode
    from tests.hermes_cli.test_kanban_quota_episode import reap
    from tests.hermes_cli.test_kanban_quota_episode_walls import _clock as _walls_clock, _hub_tier
    clock = _walls_clock(monkeypatch)
    config, _ = routing(monkeypatch)
    config['model_routing']['enabled'] = False
    _hub_tier(monkeypatch)
    codex_reset = clock[0] + 55 * 3600
    if evidence == 'provider_file':
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'openai-codex': float(codex_reset)})
        monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    else:
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {})
        monkeypatch.setattr(walls, 'live_quota_wall',
                            lambda provider, model: float(codex_reset) if provider == 'openai-codex' else None)
    monkeypatch.setattr(walls, 'live_quota_room', lambda provider, model: provider == 'anthropic')
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='moved to anthropic then walled there on A', assignee='a')
        reap(conn, tid, text='Anthropic usage limit resets in ~11h', provider='anthropic',
             fields={'route_provider': 'anthropic', 'route_model': 'claude-opus-5-5'})
        assert episode(conn, tid)['providers'] == ['anthropic']
        monkeypatch.setattr(kb, '_host_prefix', lambda: 'worker-b:')
        clock[0] += 301
        seen = []
        dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: seen.append((task.model_override, task.provider_override)))
        # Codex (the base route) is walled elsewhere: never spawn into it. Opus (room here) or a hold are fine.
        assert ('gpt-6-astra', 'openai-codex') not in seen, seen
