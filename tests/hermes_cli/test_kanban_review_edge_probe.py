"""Independent review probes, fixture boards only."""
from types import SimpleNamespace
import json
import pytest
from tests.hermes_cli.test_kanban_quota_episode import board as board  # noqa: PLC0414
from hermes_cli import kanban_db, kanban_db_connect, kanban_db_dispatch, kanban_model_tier, kanban_provider_walls
original = SimpleNamespace(kb=kanban_db, kbc=kanban_db_connect, dispatch=kanban_db_dispatch, tier=kanban_model_tier, walls=kanban_provider_walls)


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('provider_wall', [False, True])
def test_model_specific_quota_does_not_block_healthy_sibling(board, monkeypatch, all_assignees_spawnable,
                                                          reverse, provider_wall):
    from tests.hermes_cli.test_kanban_quota_episode import routing
    from tests.hermes_cli.test_kanban_quota_episode_walls import _clock
    def _class_routes(config, **by_class):
        base = dict(billing='subscription', weight=1, eligible=True, funding_verified=True,
                    capabilities={'tools': True, 'streaming': True})
        config['model_routing'] = {'enabled': True, 'routes': {
            level: [dict(base, provider=p, model=m) for p, m in routes] for level, routes in by_class.items()}}
    clock = _clock(monkeypatch)
    config, _ = routing(monkeypatch)
    routes = [('anthropic', 'claude-sonnet-5-5'), ('anthropic', 'claude-opus-5-5')]
    _class_routes(config, standard=list(reversed(routes)) if reverse else routes)
    monkeypatch.setattr(original.tier, 'load_tier_config', lambda: {'default_model': 'class:standard'})
    monkeypatch.setattr(original.walls, 'active_walls',
                        lambda **kw: {'anthropic': clock[0] + 9000} if provider_wall else {})
    monkeypatch.setattr(original.walls, 'live_quota_wall', lambda provider, model: clock[0] + 5000 if model == 'claude-sonnet-5-5' else None)
    with original.kbc.connect() as conn:
        tid = original.kb.create_task(conn, title='healthy sibling', assignee='a')
        launched = []
        result = original.dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: launched.append((task.model_override, task.provider_override)))
        if provider_wall:
            assert not launched and result.clock_held
            assert conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0] == 0
        else:
            assert launched == [('claude-opus-5-5', 'anthropic')] and not result.clock_held
            stored = original.kb.get_task(conn, tid)
            assert stored.model_override is None and stored.provider_override is None


def test_legacy_no_progress_escalation(board):
    from hermes_cli import kanban_no_progress
    with original.kbc.connect() as conn:
        tid = original.kb.create_task(conn, title='plain', assignee='default')
        conn.execute('UPDATE tasks SET title=? WHERE id=?', ('Review must use gpt-5 only', tid))
        conn.commit()
        task = original.kb.get_task(conn, tid)
        owner = kanban_no_progress._escalate(conn, task, 3, 0, 0)
        assert owner
        recovered = original.kb.get_task(conn, owner)
        assert 'Review must use gpt-5 only' in recovered.body
        assert 'superseded and not active' in recovered.body
        assert original.kb.get_task(conn, tid).title == 'Review must use gpt-5 only'


def test_historical_evidence_not_a_new_mandate(board):
    with original.kbc.connect() as conn:
        tid = original.kb.create_task(conn, title='Remove obsolete restriction', body='The old configuration required gpt-5 only. Remove that restriction.', assignee='default')
        assert tid


@pytest.mark.parametrize('text', [
    'The old configuration required gpt-5 only. Now must use grok-4.6 only.',
    'The old configuration required gpt-5 only and must use grok-4.6.',
    'Historical run instruction (superseded and not active): "must use gpt-5 only"; must use grok-4.6 only.',
    'Historical run instruction (superseded and not active): must use gpt-5 only',
    'Previous review must use gpt-5 only.',
])
def test_evidence_cannot_hide_active_mandates(board, text):
    with original.kbc.connect() as conn:
        before = conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
        with pytest.raises(ValueError, match='model-neutral'):
            original.kb.create_task(conn, title='recovery', body=text, assignee='default')
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before


def test_forwarded_directives_remain_lossless_inert_evidence(board):
    from hermes_cli.kanban_model_neutral import forwarded_text, neutral_line

    source = 'Review must use gpt-5 only. "quoted"\nrequire_model: true'
    forwarded = forwarded_text(source)
    assert forwarded_text(forwarded) == forwarded
    prefix = 'Historical run instruction (superseded and not active): '
    lines = forwarded.splitlines()
    # The named line keeps its non-model text active right after its exact history.
    assert lines[1] == neutral_line(json.loads(lines[0].removeprefix(prefix)))
    assert lines[1].startswith('Review must use any qualified route only.')
    assert '\n'.join(json.loads(line.removeprefix(prefix)) for line in (lines[0], lines[2])) == source
    with original.kbc.connect() as conn:
        tid = original.kb.create_task(conn, title='remove legacy restriction', body=forwarded,
                                      assignee='default')
        assert original.kb.get_task(conn, tid).body == forwarded

