"""Independent family probe: every resolved class obeys known quota walls."""
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch, kanban_model_tier as tier, kanban_provider_walls as walls
from tests.hermes_cli import test_kanban_quota_episode as fixtures
from tests.hermes_cli.test_kanban_quota_episode_walls import _class_routes, _clock

board = fixtures.board

@pytest.mark.parametrize('case', ['default', 'explicit', 'pattern', 'out_of_scope', 'retry'])
@pytest.mark.parametrize('walled', [True, False])
def test_resolved_class_wall(board, monkeypatch, all_assignees_spawnable, case, walled):
    clock = _clock(monkeypatch)
    config, _ = fixtures.routing(monkeypatch)
    _class_routes(config, standard=[('openai-codex', 'gpt-6-astra')], deep=[('anthropic', 'claude-opus-5-5')])
    cfg = {'default_model': 'class:standard'}
    if case == 'pattern':
        cfg['default_model_keep_patterns'] = ['complex work']
    if case == 'out_of_scope':
        cfg['default_model_profiles'] = ['other']
    monkeypatch.setattr(tier, 'load_tier_config', lambda: cfg)
    monkeypatch.setattr(walls, 'active_walls', lambda **kw: {'openai-codex': clock[0]+900, 'anthropic': clock[0]+900} if walled else {})
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *args: clock[0]+900 if walled else None)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='complex work', assignee='a', model_override='class:deep' if case == 'explicit' else None)
        if case == 'retry':
            assert kb.claim_task(conn, tid) is not None
            # Historical non-quota crash: retain its run, measure only new claims.
            conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=1 WHERE task_id=?", (tid,))
            conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, current_run_id=NULL "
                         "WHERE id=?", (tid,))
            conn.commit()
        initial_runs = conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0]
        route = tier.resolve_model_tier(conn, kb.get_task(conn, tid), lane='ready')
        expected_reason = {'default': 'default', 'explicit': 'class', 'pattern': 'pattern:complex work',
                           'out_of_scope': 'profile_out_of_scope', 'retry': 'retry_after_run_crashed'}[case]
        assert route == ('class:standard' if case in ('default', 'out_of_scope') else 'class:deep', 'auto', expected_reason)
        launched = []
        result = dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: launched.append((task.model_override, task.provider_override)))
        runs = conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0] - initial_runs
        assert (len(launched), runs) == ((0, 0) if walled else (1, 1)), (case, route, launched, runs, result)
        stored = kb.get_task(conn, tid)
        assert stored is not None
        if walled:
            assert stored.status == 'ready' and stored.claim_lock is None
            assert tid in result.clock_held
        assert (stored.model_override, stored.provider_override) == (('class:deep', 'auto') if case == 'explicit' else (None, None))
