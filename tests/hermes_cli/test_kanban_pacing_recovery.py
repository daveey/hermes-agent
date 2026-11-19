"""A far quota wall reroutes before pacing, and the destination pool decides the claim. [daveey]

(Until 2026-11-19 this file also pinned the paced one-run floor of the per-host start limit; that limit is gone:
only a pool at reserve holds a start, ``test_kanban_llm_pacing.py::test_v2_at_reserve_is_exactly_the_legacy_held_verdict``.)"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_provider_walls as walls
from tests.hermes_cli import kanban_pacing_state as pacing_state
from tests.hermes_cli import test_kanban_quota_episode as helpers
from tests.hermes_cli.test_kanban_quota_episode_walls import LIVE_CFG, _clock

board = helpers.board


@pytest.mark.parametrize('destination', [lp.PACED, lp.HELD])
def test_far_wall_reroutes_before_pacing_and_destination_limits_claims(
        board, monkeypatch, all_assignees_spawnable, destination):
    clock = _clock(monkeypatch)
    config, _ = helpers.routing(monkeypatch)
    route = config['model_routing']['routes']['standard'][0]
    config['model_routing']['routes'] = {'deep': [dict(route, model='claude-opus-5-5'),
        dict(route, provider='openai-codex', model='gpt-6-astra')]}
    monkeypatch.setattr(tier, 'load_tier_config', lambda: LIVE_CFG)
    monkeypatch.setattr(walls, 'active_walls', lambda **kw: {'openai-codex': float(clock[0] + 6 * 86400)})
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *a, **kw: None)
    monkeypatch.setattr(pd, 'load_config', lambda: lp.PacingConfig())
    consulted = []

    def pace_for(conn, provider, cfg, now):
        consulted.append(provider)
        state = destination if provider == 'anthropic' else lp.HELD
        pacing_state.store((provider, state, now + 86400), now=now)
        return lp.PoolPace(provider, state, 0.15)

    monkeypatch.setattr(pd, 'pace_for', pace_for)
    # Observe claim routing, not the independent all-provider maintenance read.
    monkeypatch.setattr(pd, 'refresh_pools', lambda conn: None)
    with kbc.connect() as conn:
        ids = [kb.create_task(conn, title=f'fresh default {i}', assignee='a') for i in range(2)]
        seen = []
        result = dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: seen.append(
            (task.id, task.model_override, task.provider_override)))
        assert consulted and set(consulted) == {'anthropic'}
        if destination == lp.HELD:
            assert seen == []
            assert len(result.llm_paced) == 2
        else:  # a paced destination holds no start: both cards run there
            assert sorted(t for t, *_ in seen) == sorted(ids)
            assert {tuple(route) for _, *route in seen} == {('claude-opus-5-5', 'anthropic')}
            assert result.llm_paced == []
            receipts = [e.payload for e in kb.list_events(conn, seen[0][0]) if e.kind == 'run_route']
            assert receipts[-1]['tier_reason'] == 'default+wall:openai-codex'
