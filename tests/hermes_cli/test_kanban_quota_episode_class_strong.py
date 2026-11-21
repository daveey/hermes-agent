"""A strong-tier class route (``class:deep``/``auto``) waits only while every route of its class is walled.

worker-6 2026-11-12 (t_01a1d016bd9f3b32): t_01a1cf7624ae27d9 crashed once, so it resolved to the strong tier
``('class:deep', 'auto', 'retry_after_run_crashed')``. A later run hit the Codex wall (until 11-18). ``decision``
held it there although class:deep's anthropic route served live: ``_base_route`` gives no target for a class,
and ``alternate_route``/``_strong_fallback`` accepted only ``default`` reasons. Every worker's pickup recorded
``quota_hold`` and the placement layer kept re-placing the card ("Pickup queue full").
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_provider_walls as walls
from hermes_cli.kanban_quota_episode import decision
from tests.hermes_cli.test_kanban_quota_episode import board as board, reap, routing  # noqa: PLC0414 -- the self-alias is load-bearing: it suppresses F811 for the pytest fixture parameter shadowing this import
from tests.hermes_cli.test_kanban_quota_episode_walls import _clock, _codex_wall

CLASS_CFG = {'default_model': 'gpt-6-astra', 'default_provider': 'openai-codex',
             'strong_model': 'class:deep', 'strong_provider': 'auto'}


def _deep_routes(monkeypatch):
    config, _ = routing(monkeypatch)
    route = config['model_routing']['routes']['standard'][0]
    config['model_routing']['routes'] = {'deep': [dict(route, provider='openai-codex', model='gpt-6-astra'),
                                                  dict(route, model='claude-opus-5-5')]}
    monkeypatch.setattr(tier, 'load_tier_config', lambda: CLASS_CFG)
    return config


def _serving(monkeypatch, served):
    monkeypatch.setattr(walls, 'live_pool_serving', lambda provider, model=None: provider in served)
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    monkeypatch.setattr(walls, 'live_quota_room', lambda *_a, **_k: False)


def _walled_card(conn, clock, *, lane='ready', title='retry after a crash'):
    tid = kb.create_task(conn, title=title, assignee='a')
    if lane == 'ready':
        # Run 1 crashed (HTTP 401): every later run resolves to the strong tier.
        conn.execute("INSERT INTO task_runs(task_id,started_at,ended_at,status,outcome) "
                     "VALUES (?,?,?,'crashed','crashed')", (tid, clock[0] - 20, clock[0] - 10))
    else:
        from tests.hermes_cli.test_kanban_model_tier import _review_source
        _review_source(conn, tid)
    conn.commit()
    reap(conn, tid, lane=lane, admit=CLASS_CFG, **_codex_wall(clock[0] + 55 * 3600))
    clock[0] += 301
    return tid


def test_retry_card_on_class_route_takes_the_free_provider_of_its_class(board, monkeypatch, all_assignees_spawnable):
    clock = _clock(monkeypatch)
    _deep_routes(monkeypatch)
    _serving(monkeypatch, {'anthropic'})
    monkeypatch.setattr(walls, 'active_walls', lambda **_k: {})
    with kbc.connect() as conn:
        tid = _walled_card(conn, clock)
        task = kb.get_task(conn, tid)
        base = tier.resolve_model_tier(conn, task, lane='ready')
        assert base == ('class:deep', 'auto', 'retry_after_run_crashed')
        assert decision(conn, task, lane='ready') == (None, ('claude-opus-5-5', 'anthropic'))
        assert dispatch.check_respawn_guard(conn, tid) is None
        seen = []
        dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: seen.append((task.model_override, task.provider_override)))
        assert seen == [('claude-opus-5-5', 'anthropic')]
        route = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='run_route' "
                             "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0]
        assert 'retry_after_run_crashed+quota_reroute' in route


@pytest.mark.parametrize('why', ['walled_elsewhere', 'not_serving', 'provider_disabled'])
def test_retry_card_keeps_its_hold_while_every_class_route_is_closed(board, monkeypatch, why):
    clock = _clock(monkeypatch)
    config = _deep_routes(monkeypatch)
    _serving(monkeypatch, set() if why == 'not_serving' else {'anthropic'})
    active = {'anthropic': float(clock[0] + 3600)} if why == 'walled_elsewhere' else {}
    monkeypatch.setattr(walls, 'active_walls', lambda **_k: active)
    if why == 'provider_disabled':
        config['providers'] = {'anthropic': {'enabled': False}}
    with kbc.connect() as conn:
        tid = _walled_card(conn, clock)
        assert decision(conn, kb.get_task(conn, tid), lane='ready') == ('subscription_quota_reset', None)
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'


def test_pattern_strong_class_card_takes_the_free_provider(board, monkeypatch):
    clock = _clock(monkeypatch)
    _deep_routes(monkeypatch)
    _serving(monkeypatch, {'anthropic'})
    monkeypatch.setattr(walls, 'active_walls', lambda **_k: {})
    cfg = {**CLASS_CFG, 'default_model_keep_patterns': ['security']}
    monkeypatch.setattr(tier, 'load_tier_config', lambda: cfg)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='security work', assignee='a')
        reap(conn, tid, admit=cfg, **_codex_wall(clock[0] + 55 * 3600))
        clock[0] += 301
        assert decision(conn, kb.get_task(conn, tid), lane='ready') == (None, ('claude-opus-5-5', 'anthropic'))


def test_unpinned_class_review_takes_the_free_provider_but_a_chosen_reviewer_waits(
        board, monkeypatch, all_assignees_spawnable):
    clock = _clock(monkeypatch)
    config = _deep_routes(monkeypatch)
    config['model_routing']['routes']['standard'] = [dict(
        config['model_routing']['routes']['deep'][1], model='claude-sonnet-5-5')]
    _serving(monkeypatch, {'anthropic'})
    monkeypatch.setattr(walls, 'active_walls', lambda **_k: {})
    with kbc.connect() as conn:
        tid = _walled_card(conn, clock, lane='review', title='legacy unpinned review')
        # The failed review allocation discovered this provider wall; the next review resolver sees it.
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'openai-codex': clock[0] + 55 * 3600})
        task = kb.get_task(conn, tid)
        assert tier.resolve_model_tier(conn, task, lane='review') == (
            'claude-opus-5-5', 'anthropic', 'review_unpinned')
        # Dynamic review resolution already chose the free independent route, so no second reroute is needed.
        assert decision(conn, task, lane='review') == (None, None)
        # A reviewer chosen for independence (review_unpinned) never moves off its walled provider.
        chosen = ('gpt-6-astra', 'openai-codex', 'review_unpinned')
        assert decision(conn, task, lane='review', base=chosen) == ('subscription_quota_reset', None)
        # Nor does the review lane take the default tier's class reroute.
        default = ('gpt-6-astra', 'openai-codex', 'default')
        assert decision(conn, task, lane='ready', base=default) == (None, ('claude-opus-5-5', 'anthropic'))  # control
        assert decision(conn, task, lane='review', base=default) == ('subscription_quota_reset', None)


@pytest.mark.parametrize('base', [
    ('claude-opus-5-5', 'anthropic', 'retry_after_run_crashed'),  # concrete strong tier: _strong_fallback's job
    ('claude-opus-5-5', 'auto', 'retry_after_run_crashed'),  # a concrete model is no class
    ('class:deep', 'openai-codex', 'retry_after_run_crashed'),  # a class bound to one provider
    ('class:deep', 'auto', 'unknown_rule'),  # only the strong-tier reasons
    ('class:deep', 'auto', 'pinned'),
])
def test_only_unpinned_strong_class_routes_move(board, monkeypatch, base):
    clock = _clock(monkeypatch)
    _deep_routes(monkeypatch)
    _serving(monkeypatch, {'anthropic'})
    monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'anthropic': float(clock[0] + 3600)}
                        if base[1] == 'anthropic' else {})
    with kbc.connect() as conn:
        tid = _walled_card(conn, clock)
        task = kb.get_task(conn, tid)
        assert decision(conn, task, lane='ready', base=base) == ('subscription_quota_reset', None)
