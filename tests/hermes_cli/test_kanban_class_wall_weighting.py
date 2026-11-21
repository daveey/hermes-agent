"""Unrelated provider walls must preserve deferred weighted class allocation."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_provider_walls as walls
from tests.hermes_cli.test_kanban_quota_episode import board as board, routing  # noqa: PLC0414
from tests.hermes_cli.test_kanban_quota_episode_walls import _class_routes, _clock


@pytest.mark.parametrize('unrelated_wall', [False, True])
def test_unrelated_wall_preserves_weighted_class_dispatch(board, monkeypatch, all_assignees_spawnable,
                                                         unrelated_wall):
    """The reviewer's real-dispatch regression and no-wall control, unchanged in substance."""
    clock = _clock(monkeypatch)
    config, _ = routing(monkeypatch)
    _class_routes(config, standard=[('openai-codex', 'gpt-6-astra'), ('anthropic', 'claude-opus-5-5')])
    from hermes_cli import config_effective
    monkeypatch.setattr(config_effective, 'load_user_config_effective', lambda **kw: config)
    monkeypatch.setattr(tier, 'load_tier_config', lambda: {'default_model': 'class:standard'})
    monkeypatch.setattr(walls, 'active_walls',
                        lambda **kw: {'xai-oauth': clock[0] + 900} if unrelated_wall else {})
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *args: None)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='Normal standard work', assignee='a')
        launched = []
        result = dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: launched.append(
            (task.model_override, task.provider_override)))
        assert [(m, p or 'auto') for m, p in launched] == [('class:standard', 'auto')], (launched, result)
        stored = kb.get_task(conn, tid)
        assert stored is not None
        assert stored.model_override is None and stored.provider_override is None


@pytest.mark.parametrize('extra', [False, True])
def test_class_wall_evidence_is_scoped_to_eligible_candidates(monkeypatch, extra):
    from types import SimpleNamespace
    from hermes_cli import kanban_class_walls as probe
    from hermes_cli import kanban_model_assignment as assignment
    config = {}
    _class_routes(config, standard=[('openai-codex', 'gpt-6-astra'), ('anthropic', 'claude-opus-5-5')])
    monkeypatch.setattr(assignment, '_assignee_config', lambda who: config)
    monkeypatch.setattr(walls, 'active_walls', lambda **kw: {} if extra else {'xai-oauth': 1800000900})
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *args: None)
    routes, known = probe.class_wall_routes(SimpleNamespace(assignee='a'), 'class:standard',
                                          now=1800000000,
                                          extra_walls={'xai-oauth': 1800000900} if extra else None)
    assert len(routes) == 2
    assert known == {}
