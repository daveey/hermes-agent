"""Independent dispatch proof of explicit class walls."""
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch, kanban_model_tier as tier, kanban_provider_walls as walls
from tests.hermes_cli import test_kanban_quota_episode as fixtures
from tests.hermes_cli.test_kanban_quota_episode_walls import _class_routes, _clock

board = fixtures.board
routing = fixtures.routing

@pytest.mark.parametrize('selector', [None, 'class:standard', 'class:deep'])
def test_every_class_wall_refuses_before_claim(board, monkeypatch, all_assignees_spawnable, selector):
    clock = _clock(monkeypatch)
    config, _ = routing(monkeypatch)
    _class_routes(config, standard=[('openai-codex', 'gpt-6-astra')], deep=[('anthropic', 'claude-opus-5-5')])
    monkeypatch.setattr(tier, 'load_tier_config', lambda: {'default_model': 'class:standard'})
    monkeypatch.setattr(walls, 'active_walls', lambda **kw: {'openai-codex': clock[0]+900, 'anthropic': clock[0]+900})
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *args: clock[0]+900)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='class wall review probe', assignee='a', model_override=selector)
        launched = []
        result = dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: launched.append((task.model_override, task.provider_override)))
        runs = conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0]
        assert (launched, runs) == ([], 0), (selector, launched, runs, result)
        assert kb.get_task(conn, tid).status == 'ready'
        assert result.pinned_quota_held


@pytest.mark.parametrize('selector', [None, 'class:standard', 'class:deep'])
@pytest.mark.parametrize('reverse', [False, True])
def test_healthy_same_provider_peer_keeps_class_admissible(board, monkeypatch, all_assignees_spawnable, selector, reverse):
    clock = _clock(monkeypatch)
    config, _ = routing(monkeypatch)
    peers = [('openai-codex', 'gpt-6-astra'), ('openai-codex', 'gpt-6-luna')]
    if reverse:
        peers.reverse()
    _class_routes(config, standard=peers, deep=peers)
    monkeypatch.setattr(tier, 'load_tier_config', lambda: {'default_model': 'class:standard'})
    monkeypatch.setattr(walls, 'active_walls', lambda **kw: {})
    monkeypatch.setattr(walls, 'live_quota_wall', lambda provider, model: clock[0]+900 if model == 'gpt-6-astra' else None)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='healthy class peer', assignee='a', model_override=selector)
        launched = []
        result = dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: launched.append(task.id))
        runs = conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0]
        assert (launched, runs) == ([tid], 1), result
        assert not result.pinned_quota_held

