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
    kb.init_db()
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        yield conn, home


@pytest.mark.parametrize('provider', [None, 'openai-codex'])
def test_create_rejects_claude_on_codex_before_inserting(board, provider):
    conn, _ = board
    with pytest.raises(ValueError, match='model_provider_mismatch'):
        kb.create_task(conn, title='Review', assignee='worker',
                       model_override='claude-opus-5-5', provider_override=provider)
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


@pytest.mark.parametrize('provider', ['anthropic', 'openrouter', 'custom'])
def test_compatible_or_open_model_provider_is_preserved(board, provider):
    conn, _ = board
    tid = kb.create_task(conn, title='Review', assignee='worker',
                         model_override='claude-opus-5-5', provider_override=provider)
    task = kb.get_task(conn, tid)
    assert (task.model_override, task.provider_override) == ('claude-opus-5-5', provider)


def test_worker_created_child_has_typed_model_rejection(board, monkeypatch):
    import json
    from tools import kanban_tools as tools
    conn, _ = board
    parent = kb.create_task(conn, title='Parent', assignee='worker')
    monkeypatch.setenv('HERMES_KANBAN_TASK', parent)
    monkeypatch.setattr(tools, '_persisted_identity', lambda: 'worker')
    result = json.loads(tools._handle_create({'title': 'Review child',
                                             'model': 'claude-opus-5-5'}))
    assert result['code'] == 'model_provider_mismatch'
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_dispatch_routes_legacy_incompatible_pin_without_spawning(board, monkeypatch, lane):
    from hermes_cli import kanban_session_activity as activity
    conn, _ = board
    tid = kb.create_task(conn, title='Legacy review', assignee='worker', session_id='chat')
    conn.execute('UPDATE tasks SET model_override=?, status=? WHERE id=?', ('claude-opus-5-5', lane, tid))
    conn.commit()
    spawned = []
    from hermes_cli.kanban_db_dispatch import dispatch_once
    dispatch_once(conn, spawn_fn=lambda *args: spawned.append(args), max_spawn=1)
    assert spawned == []
    task = kb.get_task(conn, tid)
    assert task.status == 'todo'
    assert task.block_kind == 'assignment'
    assert task.assignee == 'supervisor'
    repair = conn.execute('SELECT parent_id FROM task_links WHERE child_id=?', (tid,)).fetchone()[0]
    assert kb.get_task(conn, repair).status == 'ready'
    assert activity._replica_bucket({'status': task.status, 'block_kind': task.block_kind}) == 'active'
    with pytest.raises(ValueError, match='model_provider_mismatch'):
        kb.complete_task(conn, repair, result='Not repaired yet')
    kb.set_model_override(conn, tid, 'gpt-5', provider='openai-codex')
    assert kb.complete_task(conn, repair, result='Repaired with subscription model')
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == lane


def test_unavailable_supervisor_does_not_abort_unrelated_dispatch(board):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    conn, home = board
    (home / 'config.yaml').write_text('kanban:\n  orchestrator_profile: missing\n')
    bad = kb.create_task(conn, title='Bad pin', assignee='worker', priority=100)
    good = kb.create_task(conn, title='Good', assignee='worker')
    conn.execute('UPDATE tasks SET model_override=? WHERE id=?', ('claude-opus-5-5', bad))
    conn.commit()
    spawned = []
    dispatch_once(conn, spawn_fn=lambda task, *_args: spawned.append(task.id), max_spawn=1)
    assert spawned == [good]
    assert kb.get_task(conn, bad).status == 'ready'
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
    assert (tid, 'model_provider_mismatch') in result.respawn_guarded
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

