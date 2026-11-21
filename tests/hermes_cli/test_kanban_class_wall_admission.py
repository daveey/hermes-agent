"""Class selectors must not hide known provider walls at actual dispatch."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_provider_walls as walls
from tests.hermes_cli.test_kanban_quota_episode import board as board, routing  # noqa: PLC0414
from tests.hermes_cli.test_kanban_quota_episode_walls import _class_routes, _clock


def test_class_wall_lookup_observes_sequential_assignee_registries(monkeypatch):
    """A long-lived dispatcher must read the current registry on every call."""
    from importlib import reload

    from hermes_cli import kanban_class_walls as probe
    from hermes_cli import kanban_model_assignment as assignment

    first = {}
    _class_routes(first, standard=[('openai-codex', 'gpt-6-astra')])
    monkeypatch.setattr(assignment, '_assignee_config', lambda who: first)
    reload(probe)  # first registry is present when the module is initialized
    monkeypatch.setattr(walls, 'active_walls', lambda **kw: {})
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *args: None)
    task = SimpleNamespace(assignee='a')
    for provider, model in [('openai-codex', 'gpt-6-astra'), ('anthropic', 'claude-opus-5-5')]:
        config = {}
        _class_routes(config, standard=[(provider, model)])
        monkeypatch.setattr(assignment, '_assignee_config', lambda who, current=config: current)
        routes, blocked = probe.class_wall_routes(task, 'class:standard', now=1_800_000_000)
        assert [(r['provider'], r['model']) for r in routes] == [(provider, model)]
        assert blocked == {}


@pytest.mark.parametrize('evidence', ['file', 'live'])
@pytest.mark.parametrize('all_blocked', [False, True])
def test_actual_class_admission_avoids_known_walls(board, monkeypatch, all_assignees_spawnable,
                                                 evidence, all_blocked):
    clock = _clock(monkeypatch)
    config, _ = routing(monkeypatch)
    _class_routes(config, standard=[('openai-codex', 'gpt-6-astra'), ('anthropic', 'claude-opus-5-5')])
    monkeypatch.setattr(tier, 'load_tier_config', lambda: {'default_model': 'class:standard'})
    blocked = {'openai-codex': clock[0] + 5000}
    if all_blocked:
        blocked['anthropic'] = clock[0] + 900
    monkeypatch.setattr(walls, 'active_walls', lambda **kw: blocked if evidence == 'file' else {})
    monkeypatch.setattr(walls, 'live_quota_wall',
                        lambda provider, model: blocked.get(provider) if evidence == 'live' else None)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='class admission', assignee='a')
        launched = []
        result = dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: launched.append(
            (task.model_override, task.provider_override)))
        if all_blocked:
            assert launched == []
            assert conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0] == 0
            assert result.clock_held
        else:
            assert launched == [('claude-opus-5-5', 'anthropic')]
            stored = kb.get_task(conn, tid)
            assert stored is not None
            assert stored.model_override is None and stored.provider_override is None
            receipt = kb._json_dict(conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='run_route' ORDER BY id DESC LIMIT 1",
                (tid,)).fetchone()[0])
            assert receipt['provider'] == 'anthropic'
