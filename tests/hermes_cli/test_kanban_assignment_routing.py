"""Durable assignment repair stays agent-owned without swallowing human asks."""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_session_activity as activity


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    (home / 'config.yaml').write_text('kanban:\n  orchestrator_profile: supervisor\n')
    for name in ('worker', 'supervisor', 'creator'):
        profile = home / 'profiles' / name
        profile.mkdir(parents=True)
        (profile / 'config.yaml').write_text('platform_toolsets:\n  cli: [terminal, file, kanban]\nterminal:\n  backend: local\n')
    kb.init_db()
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        yield conn, home


def task_with_drift(board):
    conn, home = board
    tid = kb.create_task(conn, title='Fix repo', assignee='worker', created_by='creator',
                         requires=['terminal'], session_id='chat')
    (home / 'profiles/worker/config.yaml').write_text('platform_toolsets:\n  cli: [web]\n')
    return tid


def payload(conn, tid):
    return json.loads(conn.execute('SELECT payload FROM task_events WHERE task_id=? AND kind IN ("blocked", "block_loop_detected") ORDER BY id DESC', (tid,)).fetchone()[0])


@pytest.mark.parametrize('kind', ['assignment', 'capability'])
def test_routes_real_mismatch_to_durable_supervisor(board, kind):
    conn, _ = board
    tid = task_with_drift(board)
    assert kb.block_task(conn, tid, kind=kind, reason='Worker lacks terminal tools')
    original = kb.get_task(conn, tid)
    assert original.status == 'todo'
    assert original.assignee == 'supervisor'
    assert original.requires == ['terminal']
    assert original.block_kind == 'assignment'
    audit = payload(conn, tid)
    assert audit['requested_kind'] == kind
    assert audit['rekind_reason'] == ('profile_capability_mismatch' if kind == 'capability' else None)
    supervisor = kb.get_task(conn, audit['supervisor_task_id'])
    assert supervisor.assignee == 'supervisor'
    assert supervisor.status == 'ready'
    assert not kb._parents_satisfied(conn, tid)
    assert kb._parents_satisfied(conn, supervisor.id)
    path = Path(conn.execute('PRAGMA database_list').fetchone()[2])
    assert activity._read_counts(path).get('chat') == 2
    assert activity._read_counts(path, activity.BLOCKED_STATUSES).get('chat', 0) == 0
    assert activity._replica_bucket({'status': 'blocked', 'block_kind': 'assignment'}) == 'active'
    assert not kb.block_task(conn, tid, kind=kind, reason='retry')
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 2


@pytest.mark.parametrize('parked', [False, True], ids=['ready', 'breaker-parked'])
@pytest.mark.parametrize('kind', ['capability', 'assignment'])
@pytest.mark.parametrize('reason', ['Need credentials', 'Please sign in to GitHub', 'Need API key',
                                  'Need user approval', 'Payment method expired', 'Policy decision required'])
def test_real_human_access_request_not_rekinded(board, reason, kind, parked):
    conn, _ = board
    tid = task_with_drift(board)
    if parked:
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        conn.commit()
    assert kb.block_task(conn, tid, kind=kind, reason=reason)
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.block_kind == 'capability'
    assert task.status == 'blocked'
    assert task.assignee == 'worker'
    audit = payload(conn, tid)
    assert audit['kind'] == 'capability'
    assert 'supervisor_task_id' not in audit
    if kind == 'assignment':
        assert audit['requested_kind'] == 'assignment'
        assert audit['rekind_reason'] == 'human_access_request'
    if parked:
        assert audit['classified_in_place'] is True
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


@pytest.mark.parametrize('reason', [
    'Need David to choose a deployment region',
    'Waiting for the owner to review the contract',
    'Service unavailable',
    'Worker lacks terminal tools; please choose the release scope',
    '',
])
def test_unrelated_human_or_ambiguous_reason_not_rekinded(board, reason):
    conn, _ = board
    tid = task_with_drift(board)
    assert kb.block_task(conn, tid, kind='capability', reason=reason)
    assert kb.get_task(conn, tid).block_kind == 'capability'
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


def test_supervisor_must_have_board_tools(board):
    conn, home = board
    tid = task_with_drift(board)
    (home / 'profiles/supervisor/config.yaml').write_text(
        'platform_toolsets:\n  cli: [terminal, file]\nterminal:\n  backend: local\n')
    assert kb.block_task(conn, tid, kind='assignment', reason='Wrong worker')
    assert kb.get_task(conn, tid).assignee == 'creator'
    repair = kb.get_task(conn, payload(conn, tid)['supervisor_task_id'])
    assert repair.assignee == 'creator'
    assert 'kanban' in repair.requires


def test_no_requirements_means_no_proven_mismatch(board):
    conn, _ = board
    tid = kb.create_task(conn, title='Research', assignee='worker')
    assert kb.block_task(conn, tid, kind='capability', reason='Cannot use terminal')
    assert kb.get_task(conn, tid).block_kind == 'capability'


def test_creator_fallback_and_no_self_supervision(board):
    conn, home = board
    tid = task_with_drift(board)
    (home / 'profiles/supervisor/config.yaml').write_text('platform_toolsets:\n  cli: [web]\n')
    assert kb.block_task(conn, tid, kind='assignment', reason='Wrong worker')
    repair = kb.get_task(conn, payload(conn, tid)['supervisor_task_id'])
    assert repair.assignee == 'creator'
    with pytest.raises(ValueError, match='supervisor_route_exhausted'):
        kb.block_task(conn, repair.id, kind='assignment', reason='Cannot repair')
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 2
    assert kb.get_task(conn, repair.id).status == 'ready'


def test_missing_or_self_targets_remain_visible_without_fake_routing(board):
    conn, home = board
    tid = task_with_drift(board)
    (home / 'config.yaml').write_text('kanban:\n  orchestrator_profile: missing\n')
    conn.execute('UPDATE tasks SET created_by="worker" WHERE id=?', (tid,))
    conn.commit()
    with pytest.raises(ValueError, match='no_capable_supervisor'):
        kb.block_task(conn, tid, kind='assignment', reason='Wrong profile')
    assert kb.get_task(conn, tid).status == 'ready'
    assert kb.get_task(conn, tid).assignee == 'worker'
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


def test_workspace_alone_proves_mismatch(board, tmp_path):
    conn, home = board
    tid = kb.create_task(conn, title='Repo', assignee='worker', workspace_kind='worktree',
                         workspace_path=str(tmp_path))
    (home / 'profiles/worker/config.yaml').write_text('platform_toolsets:\n  cli: [web]\n')
    assert kb.block_task(conn, tid, kind='capability', reason='Missing repo tools')
    assert kb.get_task(conn, tid).block_kind == 'assignment'


def test_classify_breaker_parked_assignment_routes(board):
    conn, _ = board
    tid = task_with_drift(board)
    conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
    conn.commit()
    assert kb.block_task(conn, tid, kind='assignment', reason='Worker mismatch')
    assert payload(conn, tid)['supervisor_task_id']
    assert kb.get_task(conn, tid).status == 'todo'


def test_supervisor_failure_rolls_back_original(board, monkeypatch):
    conn, _ = board
    tid = task_with_drift(board)
    def fail(*args, **kwargs):
        raise RuntimeError('write failed')
    monkeypatch.setattr(kb, 'create_task', fail)
    with pytest.raises(RuntimeError, match='write failed'):
        kb.block_task(conn, tid, kind='assignment', reason='Worker mismatch')
    assert kb.get_task(conn, tid).status == 'ready'
    assert kb.get_task(conn, tid).assignee == 'worker'
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1


def test_running_original_handoff_waits_then_dispatches_new_profile(board):
    conn, _ = board
    tid = task_with_drift(board)
    claimed = kb.claim_task(conn, tid, claimer='worker-run')
    assert claimed is not None
    assert kb.block_task(conn, tid, kind='assignment', reason='Wrong worker',
                         expected_run_id=claimed.current_run_id)
    original = kb.get_task(conn, tid)
    assert original.assignee == 'supervisor'
    assert original.claim_lock is None
    assert kb.claim_task(conn, tid) is None
    repair_id = payload(conn, tid)['supervisor_task_id']
    assert kb.complete_task(conn, repair_id, result='Verified original assignment and requirements')
    kb.recompute_ready(conn)
    resumed = kb.claim_task(conn, tid, claimer='supervisor-run')
    assert resumed is not None
    assert resumed.assignee == 'supervisor'


def test_repair_completion_rechecks_policy_drift(board):
    from hermes_cli.kanban_capabilities import CapabilityMismatchError
    conn, home = board
    tid = task_with_drift(board)
    assert kb.block_task(conn, tid, kind='assignment', reason='Wrong worker')
    repair_id = payload(conn, tid)['supervisor_task_id']
    (home / 'profiles/supervisor/config.yaml').write_text('platform_toolsets:\n  cli: [web]\n')
    with pytest.raises(CapabilityMismatchError):
        kb.complete_task(conn, repair_id, result='Verified')
    assert kb.get_task(conn, tid).status == 'todo'
    assert kb.get_task(conn, repair_id).status != 'done'


def test_completed_repair_does_not_spawn_an_endless_supervisor_chain(board):
    conn, _ = board
    tid = task_with_drift(board)
    assert kb.block_task(conn, tid, kind='assignment', reason='Wrong worker')
    repair_id = payload(conn, tid)['supervisor_task_id']
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (repair_id,))
    conn.commit()
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == 'ready'
    with pytest.raises(ValueError, match='supervisor_route_exhausted'):
        kb.block_task(conn, tid, kind='assignment', reason='Still wrong worker')
    assert kb.get_task(conn, tid).status == 'ready'
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 2


@pytest.mark.parametrize('surface', ['tool', 'cli'])
def test_assignment_block_entry_points_route(board, monkeypatch, surface):
    import argparse
    from hermes_cli import kanban as cli
    from tools import kanban_tools as tools
    from tools.registry import registry
    conn, _ = board
    tid = task_with_drift(board)
    if surface == 'tool':
        schema = registry.get_schema('kanban_block')
        assert 'assignment' in schema['parameters']['properties']['kind']['enum']
        monkeypatch.setattr(tools, '_worker_guard', lambda *args: tid)
        monkeypatch.setattr(tools, '_worker_run_id', lambda *args: None)
        conn.execute('UPDATE tasks SET goal_mode=1 WHERE id=?', (tid,))
        conn.commit()
        result = json.loads(registry.dispatch('kanban_block', {
            'task_id': tid, 'kind': 'assignment', 'reason': 'Profile mismatch',
        }))
        assert result.get('ok'), result
    else:
        parser = argparse.ArgumentParser()
        cli.build_parser(parser.add_subparsers())
        args = parser.parse_args(['kanban', 'block', tid, 'Profile mismatch',
                                 '--kind', 'assignment'])
        assert cli._cmd_block(args) == 0
    assert kb.get_task(conn, tid).status == 'todo'
    assert payload(conn, tid)['supervisor_task_id']


def test_repair_failure_has_typed_tool_error(board, monkeypatch):
    from tools import kanban_tools as tools
    from tools.registry import registry
    conn, home = board
    tid = task_with_drift(board)
    (home / 'config.yaml').write_text('kanban:\n  orchestrator_profile: missing\n')
    conn.execute('UPDATE tasks SET created_by="worker" WHERE id=?', (tid,))
    conn.commit()
    monkeypatch.setattr(tools, '_worker_guard', lambda *args: tid)
    monkeypatch.setattr(tools, '_worker_run_id', lambda *args: None)
    result = json.loads(registry.dispatch('kanban_block', {
        'task_id': tid, 'kind': 'assignment', 'reason': 'Wrong profile',
    }))
    assert result['code'] == 'no_capable_supervisor'
    assert kb.get_task(conn, tid).status == 'ready'


def test_stale_run_does_not_create_supervisor(board):
    conn, _ = board
    tid = task_with_drift(board)
    assert not kb.block_task(conn, tid, kind='capability', reason='No terminal', expected_run_id=9999)
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
