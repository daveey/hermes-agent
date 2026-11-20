"""Source custody crosses the real managed Popen, not only the dispatcher submitter."""
import json
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.gateway.fixtures.local_recovery_probe import child_env

KEYS = ('HERMES_KANBAN_SOURCE_PATH', 'HERMES_KANBAN_SOURCE_COMMIT', 'HERMES_KANBAN_SOURCE_BRANCH')
COMMIT = '1' * 40


@pytest.fixture
def claim(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from gateway.session_contract import SessionActor
    from gateway.session_kanban import build_kanban_policy
    for key in ('HERMES_HOME', 'HERMES_KANBAN_HOME'):
        monkeypatch.setenv(key, str(tmp_path))
    for key in ('HERMES_KANBAN_DB', 'HERMES_DELEGATED_CHILD'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env', lambda _: str(tmp_path))
    kb.create_board('source-env')
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    with closing(connect(board='source-env')) as conn:
        tid = kb.create_task(conn, title='source environment regression', assignee='default')
        task = kb.claim_task(conn, tid)
        from hermes_cli.kanban_db_workspace import set_workspace_path
        assert task is not None
        set_workspace_path(conn, tid, str(scratch))
        db = conn.execute('PRAGMA database_list').fetchone()[2]
    actor = SessionActor('owner', str(tmp_path), frozenset({'session:create'}), 'transport')
    connection = SimpleNamespace(authority=SimpleNamespace(profile_id=str(tmp_path)), actor=actor, native_owner=True)
    params = dict(board='source-env', task_id=tid, run_id=task.current_run_id, claim_lock=task.claim_lock, db=db)
    policy, _ = build_kanban_policy(connection, params, {'model': {'default': 'test', 'provider': 'custom'}})
    # Unrelated placement/transport gates are independently covered. Real source lookup and Popen remain.
    monkeypatch.setattr('gateway.session_kanban_scope.resolve_run_scope', lambda _: None)
    monkeypatch.setattr('gateway.session_kanban_capsule.check_capsule_transport', lambda _: None)
    monkeypatch.setattr('gateway.session_kanban_capsule.attach_capsule_transport', lambda _: None)
    monkeypatch.setattr('hermes_cli.worker_compute_capacity.managed_target', lambda *a: None)
    return SimpleNamespace(policy=policy, task=task, db=db, scratch=scratch, root=tmp_path)


def attach(claim, mode, **changes):
    import sqlite3
    from hermes_cli.kanban_source_board import AttachedRun, record_run
    with closing(sqlite3.connect(claim.db)) as conn:
        uid = 'source-env-run'
        run = AttachedRun(claim.task.current_run_id, claim.task.id, uid, mode, 1,
                          'wt/source-test', str(claim.root / 'external'), COMMIT, None, 'attached',
                          source_protocol=2)
        record_run(conn, replace(run, **changes))


def spawn(claim, monkeypatch, *, stale=False, probe=False):
    from gateway.session_kanban_scope import placing_spawn
    env = child_env()
    home = claim.root / 'child-home'
    home.mkdir(exist_ok=True)
    state = home / '.hermes'
    state.mkdir(exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(state),
               PYTHONPATH=str(Path(__file__).resolve().parents[2]), TERMINAL_CWD=str(claim.scratch))
    if stale:
        env.update(dict.fromkeys(KEYS, 'stale-submitters-source'))
    script = Path(__file__).parent / 'fixtures' / 'kanban_source_env_probe.py'
    proc = placing_spawn(subprocess.Popen, claim.policy)(
        [sys.executable, str(script), 'tools' if probe else 'startup'], env=env, cwd=claim.scratch,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
    try:
        out, err = proc.communicate(timeout=90)
    except BaseException:
        proc.kill()
        proc.communicate()
        raise
    assert proc.returncode == 0, out + err
    return json.loads(out)


@pytest.mark.parametrize('mode', ['materialized', 'holder_reuse'])
def test_attached_source_reaches_actual_startup_and_both_tools(claim, monkeypatch, mode):
    attach(claim, mode)
    expected = dict(zip(KEYS, (str(claim.root / 'external'), COMMIT, 'wt/source-test')))
    result = spawn(claim, monkeypatch, probe=True)
    for surface in ('startup', 'terminal', 'execute_code'):
        assert result[surface]['source'] == expected, (surface, result)
        assert result[surface]['cwd'] == str(claim.scratch)
    assert result['startup']['source']['HERMES_KANBAN_SOURCE_PATH'] != str(claim.scratch)


@pytest.mark.parametrize('state', [None, 'pending', 'aborted'])
def test_unattached_scratch_strips_inherited_source(claim, monkeypatch, state):
    if state is not None:
        attach(claim, 'holder_reuse', state=state)
    result = spawn(claim, monkeypatch, stale=True, probe=True)
    assert all(result[surface]['source'] == {} for surface in ('startup', 'terminal', 'execute_code'))


@pytest.mark.parametrize('change', ['source_task', 'task_claim', 'run_claim', 'run_status', 'run_ended'])
def test_invalid_source_claim_refuses_before_spawn(claim, change):
    import sqlite3
    from hermes_state_runtime import RuntimeStoreError
    from gateway.session_kanban_scope import placing_spawn
    attach(claim, 'holder_reuse', **({'task_id': 'foreign-task'} if change == 'source_task' else {}))
    with closing(sqlite3.connect(claim.db)) as conn:
        mutations = {
            'task_claim': ('UPDATE tasks SET claim_lock=? WHERE id=?', ('new-claim', claim.task.id)),
            'run_claim': ('UPDATE task_runs SET claim_lock=? WHERE id=?', ('new-claim', claim.task.current_run_id)),
            'run_status': ('UPDATE task_runs SET status=? WHERE id=?', ('done', claim.task.current_run_id)),
            'run_ended': ('UPDATE task_runs SET ended_at=1 WHERE id=?', (claim.task.current_run_id,)),
        }
        if change in mutations:
            conn.execute(*mutations[change])
        conn.commit()
    called = []
    with pytest.raises(RuntimeStoreError, match='^stale_kanban_source_claim$'):
        placing_spawn(lambda *a, **kw: called.append(kw), claim.policy)([], env={})
    assert called == []


def test_source_env_copies_ambient_and_does_not_mutate_gateway(claim, monkeypatch):
    from gateway.session_kanban_source_env import source_worker_env
    import os
    attach(claim, 'holder_reuse')
    for key in KEYS:
        monkeypatch.setenv(key, 'stale')
    result = source_worker_env(claim.policy, None)
    assert result[KEYS[0]] == str(claim.root / 'external')
    assert all(os.environ[key] == 'stale' for key in KEYS)
    provided = {'PATH': '/usr/bin', 'HERMES_KANBAN_SOURCE_PATH': 'stale'}
    result = source_worker_env(claim.policy, provided)
    assert result['PATH'] == '/usr/bin' and provided[KEYS[0]] == 'stale'


def test_non_kanban_env_unchanged():
    from gateway.session_kanban_source_env import source_worker_env
    env = {'HERMES_KANBAN_SOURCE_PATH': 'operator-value'}
    assert source_worker_env(None, env) is env
    assert source_worker_env(SimpleNamespace(kanban_json=None), None) is None


def test_unreadable_source_database_refuses_before_spawn(claim):
    from hermes_state_runtime import RuntimeStoreError
    from gateway.session_kanban_scope import placing_spawn
    context = json.loads(claim.policy.kanban_json)
    context['db'] = str(claim.root / 'missing.db')
    called = []
    with pytest.raises(RuntimeStoreError, match='^invalid_kanban_source_claim$'):
        placing_spawn(lambda *a, **kw: called.append(kw),
                      replace(claim.policy, kanban_json=json.dumps(context)))([], env={})
    assert called == []
