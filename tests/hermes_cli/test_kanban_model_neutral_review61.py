"""Quota episodes never authorize structurally unusable class policy."""
import pytest

from agent.model_routing import ModelRoutingError
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch, kanban_model_tier as tier
from hermes_cli import kanban_provider_walls as walls
from hermes_cli.kanban_class_walls import pickup_refusal
from hermes_cli.kanban_quota_episode import episode
from tests.hermes_cli.test_kanban_quota_episode import board as board, routing, reap  # noqa: PLC0414
from tests.hermes_cli.test_kanban_quota_episode_walls import _clock


def disable(config, mode):
    policy = config['model_routing']
    routes = policy['routes']['standard']
    actions = {
        'disabled': lambda: policy.update(enabled=False),
        'empty': lambda: policy['routes'].update(standard=[]),
        'providers_off': lambda: config.update(providers={r['provider']: {'enabled': False} for r in routes}),
        'capabilities': lambda: [r['capabilities'].update(tools=False) for r in routes],
        'healthy': lambda: None,
    }
    actions[mode]()


@pytest.mark.parametrize('active_episode', [True, False])
@pytest.mark.parametrize('mode', ['disabled', 'empty', 'providers_off', 'capabilities', 'healthy'])
def test_episode_live_recovery_preserves_structural_gate(board, monkeypatch, all_assignees_spawnable,
                                                       active_episode, mode):
    clock = _clock(monkeypatch)
    config, _ = routing(monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='Normal work', assignee='a')
        # Historical fixture only: live policy mutations must not create this row.
        conn.execute("UPDATE tasks SET model_override='claude-opus-5-5',provider_override='anthropic',"
                     "model_pin_mandatory=1 WHERE id=?", (tid,))
        conn.commit()
        if active_episode:
            reap(conn, tid, text='usage limit resets in ~11h')
        clock[0] += 1800
        state = episode(conn, tid)
        disable(config, mode)
        monkeypatch.setattr(walls, 'live_quota_room', lambda *a: True)
        monkeypatch.setattr(walls, 'active_walls', lambda **kw: {})
        monkeypatch.setattr(walls, 'live_quota_wall', lambda *a: None)
        before = kb.get_task(conn, tid)
        runs = conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0]
        if mode != 'healthy':
            assert pickup_refusal(conn, {'id': tid, 'status': 'ready'})[0] == 'model_unroutable'
        launched = []
        result = dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: launched.append(
            (task.model_override, task.provider_override)))
        current = kb.get_task(conn, tid)
        assert bool(launched) == (mode == 'healthy')
        if mode == 'healthy':
            assert launched == [('class:standard', 'auto')]
        else:
            assert current.status == 'ready'
            assert current.claim_lock == before.claim_lock
            assert current.current_run_id == before.current_run_id
            assert current.consecutive_failures == before.consecutive_failures
            assert conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0] == runs
            assert any('model_unroutable' in why for _, why in result.skipped_ineligible)
            assert episode(conn, tid) == state
        assert (current.model_override, current.provider_override, current.model_pin_mandatory) == (None, None, None)


@pytest.mark.parametrize('mode', ['disabled', 'empty', 'providers_off', 'capabilities'])
def test_final_apply_refuses_policy_changed_after_preclaim(board, monkeypatch, mode):
    config, cfg = routing(monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='Normal work', assignee='a')
        conn.execute("UPDATE tasks SET model_override='claude-opus-5-5',provider_override='anthropic' WHERE id=?", (tid,))
        conn.commit()
        task = kb.get_task(conn, tid)
        disable(config, mode)
        # No successful return/receipt can authorize a historical selector.
        with pytest.raises(ModelRoutingError, match='unroutable'):
            tier.apply_model_tier(conn, task, lane='ready', cfg=cfg)
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='run_route'", (tid,)).fetchone()[0] == 0
