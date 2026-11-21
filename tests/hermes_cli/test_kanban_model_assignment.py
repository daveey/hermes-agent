"""Pinned models must not inherit a known incompatible worker provider."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    (home / 'config.yaml').write_text('kanban:\n  orchestrator_profile: supervisor\n')
    for name in ('worker', 'supervisor'):
        profile = home / 'profiles' / name
        profile.mkdir(parents=True)
        (profile / 'config.yaml').write_text(
            'model:\n  default: gpt-5\n  provider: openai-codex\n'
            'platform_toolsets:\n  cli: [terminal, file, kanban]\nterminal:\n  backend: local\n')
    # Lifecycle controls must reach actual admission on the executing profile.
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    for name in ('worker', 'supervisor'):
        path = home / 'profiles' / name / 'config.yaml'
        path.write_text(path.read_text() + _managed_yaml())
    kb.init_db()
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        yield conn, home


@pytest.mark.parametrize('provider', [None, 'openai-codex'])
def test_create_rejects_claude_on_codex_before_inserting(board, provider):
    conn, _ = board
    with pytest.raises(ValueError, match='model-neutral'):
        kb.create_task(conn, title='Review', assignee='worker',
                       model_override='claude-opus-5-5', provider_override=provider)
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


@pytest.mark.parametrize('provider', ['anthropic', 'openrouter', 'custom'])
def test_compatible_or_open_model_provider_is_preserved(board, provider):
    conn, _ = board
    with pytest.raises(ValueError, match='model-neutral'):
        kb.create_task(conn, title='Review', assignee='worker',
                       model_override='claude-opus-5-5', provider_override=provider)
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    tid = kb.create_task(conn, title='Review', assignee='worker', model_override='class:deep')
    task = kb.get_task(conn, tid)
    assert (task.model_override, task.provider_override) == ('class:deep', 'auto')


def test_worker_created_child_has_typed_model_rejection(board, monkeypatch):
    import json
    from tools import kanban_tools as tools
    conn, _ = board
    parent = kb.create_task(conn, title='Parent', assignee='worker')
    monkeypatch.setenv('HERMES_KANBAN_TASK', parent)
    monkeypatch.setattr(tools, '_persisted_identity', lambda: 'worker')
    result = json.loads(tools._handle_create({'title': 'Review child',
                                             'model': 'claude-opus-5-5'}))
    assert 'model-neutral' in result['error']
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_dispatch_routes_legacy_incompatible_pin_without_spawning(board, monkeypatch, lane):
    """Legacy restrictions no longer create assignment-repair work; author custody still constrains review."""
    from tests.hermes_cli.test_kanban_model_tier import _review_source
    from hermes_cli.kanban_db_dispatch import dispatch_once
    from hermes_cli.kanban_review_model import implementer_models
    conn, _ = board
    tid = kb.create_task(conn, title='Legacy review', assignee='worker', session_id='chat')
    source_run = None
    if lane == 'review':
        _review_source(conn, tid)
        source_run = conn.execute(
            'SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1', (tid,)).fetchone()[0]
    # Historical rows remain audit evidence, not an ingress path to create pins.
    conn.execute('UPDATE tasks SET model_override=?, status=? WHERE id=?', ('claude-opus-5-5', lane, tid))
    conn.commit()
    spawned = []
    result = dispatch_once(conn, spawn_fn=lambda task, *args: spawned.append(
        (task.id, task.model_override, task.provider_override)), max_spawn=1)
    if lane == 'review':
        from agent.model_routing import eligible_routes, validate_policy
        from agent.model_routing_agent import class_requirements
        from hermes_cli.kanban_model_assignment import _assignee_config
        config = _assignee_config('worker')
        assert config is not None
        validate_policy(config['model_routing'])
        qualified = {(route['model'], route['provider'])
                     for route in eligible_routes(config['model_routing'], 'standard', class_requirements(config))}
        assert len(spawned) == 1 and spawned[0][0] == tid, repr(result)
        assert spawned[0][1:] in qualified
        assert kb.get_task(conn, tid).current_run_id != source_run
        assert conn.execute('SELECT outcome FROM task_runs WHERE id=?', (source_run,)).fetchone()[0] == 'review_requested'
    else:
        assert spawned == [(tid, 'class:standard', 'auto')], repr(result)
    task = kb.get_task(conn, tid)
    assert task.status == 'running' and task.assignee == 'worker' and task.block_kind is None
    assert task.model_override == 'claude-opus-5-5'  # never rewrite historical row
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM task_links').fetchone()[0] == 0
    if lane == 'review':
        # Retain both authenticated authorship and conservative historical pin provenance.
        assert implementer_models(conn, task) == {'claude-sonnet-5-5', 'claude-opus-5-5'}
        # Independence is the distinct admitted execution above, not model diversity.


def test_unavailable_supervisor_does_not_abort_unrelated_dispatch(board):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    conn, home = board
    (home / 'config.yaml').write_text('kanban:\n  orchestrator_profile: missing\n')
    profile = home / 'profiles' / 'unconfigured'
    profile.mkdir()
    (profile / 'config.yaml').write_text('platform_toolsets:\n  cli: [terminal, file, kanban]\n')
    bad = kb.create_task(conn, title='Bad pin', assignee='unconfigured', priority=100)
    good = kb.create_task(conn, title='Good', assignee='worker')
    conn.execute('UPDATE tasks SET model_override=? WHERE id=?', ('claude-opus-5-5', bad))
    conn.commit()
    spawned = []
    result = dispatch_once(conn, spawn_fn=lambda task, *_args: spawned.append(task.id), max_spawn=1)
    assert spawned == [good], repr(result)
    assert result.skipped_ineligible == [(bad, 'model_unroutable:routing_disabled')]
    assert kb.get_task(conn, bad).status == 'ready'
    assert kb.get_task(conn, bad).current_run_id is None
    from hermes_cli.kanban_db_dispatch import guard_hold_cap_seconds
    assert guard_hold_cap_seconds('model_provider_mismatch') is None


def test_dry_run_reports_mismatch_without_writing(board):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    conn, _ = board
    tid = kb.create_task(conn, title='Legacy pin', assignee='worker')
    conn.execute('UPDATE tasks SET model_override=? WHERE id=?', ('claude-opus-5-5', tid))
    conn.commit()
    before = list(conn.iterdump())
    result = dispatch_once(conn, dry_run=True)
    assert [item[0] for item in result.spawned] == [tid], repr(result)
    assert result.respawn_guarded == []
    assert list(conn.iterdump()) == before



@pytest.mark.parametrize('backend,toolsets', [('local', '[web]'), ('docker', '[terminal, file, kanban]')])
def test_worker_child_cannot_assign_host_repo_to_incapable_profile(board, monkeypatch, backend, toolsets):
    import json
    from tools import kanban_tools as tools
    conn, home = board
    (home / 'profiles/supervisor/config.yaml').write_text(
        f'platform_toolsets:\n  cli: {toolsets}\nterminal:\n  backend: {backend}\n')
    parent = kb.create_task(conn, title='Parent', assignee='worker')
    monkeypatch.setenv('HERMES_KANBAN_TASK', parent)
    monkeypatch.setattr(tools, '_persisted_identity', lambda: 'worker')
    result = json.loads(tools._handle_create({'title': 'Host repo child', 'assignee': 'supervisor',
                                             'requires': ['terminal', 'file', 'git', 'host_repo']}))
    assert result['code'] == 'assignee_capability_mismatch'
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1

