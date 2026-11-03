"""Native owner derives the capsule requirement itself and fails closed without live transport. [daveey]

The requirement comes from the claimed task row (workspace kind / requires) plus the assignee's
actual resolved terminal backend, never from caller params, saved policy or config grants.
"""
import json
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest


def _write_backend(home, backend):
    (home / 'config.yaml').write_text(json.dumps({'terminal': {'backend': backend},
                                                  'platform_toolsets': {'cli': ['terminal', 'file']}}), encoding='utf-8')


def _board(tmp_path, monkeypatch, *, workspace_kind='dir', local=False):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from gateway.session_contract import Principal
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_DELEGATED_CHILD', raising=False)
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env',
                        lambda name: str(tmp_path) if name == 'default' else str(tmp_path / name))
    # Creation-time gate is not under test: the row is created under a local policy, then the assignee's
    # real config.yaml becomes the backend under test (as a capsule-admitted create would leave the row).
    _write_backend(tmp_path, 'local')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    kb.create_board('owned')
    kwargs = dict(workspace_kind=workspace_kind)
    if workspace_kind == 'dir':
        kwargs['workspace_path'] = str(workspace)
    with closing(connect(board='owned')) as conn:
        task_id = kb.create_task(conn, title='t', body='b', assignee='default', **kwargs)
        kb.recompute_ready(conn)
        task = kb.claim_task(conn, task_id)
        assert task is not None
        # Match dispatcher materialization before the owner reads the claimed row.
        from hermes_cli import kanban_db_workspace as kbw
        kbw.set_workspace_path(conn, task.id, str(kbw.resolve_workspace(task, board='owned')))
    _write_backend(tmp_path, 'local' if local else 'docker')
    actor = Principal('owner', str(tmp_path), frozenset({'session:create'}), 'transport')
    connection = SimpleNamespace(authority=SimpleNamespace(profile_id=str(tmp_path)), actor=actor, native_owner=True)
    params = dict(board='owned', task_id=task.id, run_id=task.current_run_id, claim_lock=task.claim_lock)
    config = {'model': {'default': 'm', 'provider': 'custom'}, 'platform_toolsets': {'cli': ['terminal', 'file']}}
    return connection, params, config


def _policy(tmp_path, monkeypatch, **kw):
    from gateway.session_kanban import build_kanban_policy
    connection, params, config = _board(tmp_path, monkeypatch, **kw)
    policy, _ = build_kanban_policy(connection, params, config)
    return policy


def test_remote_host_repo_requirement_is_derived_and_frozen(tmp_path, monkeypatch):
    policy = _policy(tmp_path, monkeypatch, local=False)
    context = json.loads(policy.kanban_json)
    assert context['capsule'] == {'required': True, 'requires': 'host_repo', 'backend': 'remote'}


@pytest.mark.parametrize('kw', [dict(local=True), dict(workspace_kind='scratch', local=False)])
def test_local_or_scratch_remote_not_required(tmp_path, monkeypatch, kw):
    from gateway.session_kanban_capsule import require_capsule_transport
    policy = _policy(tmp_path, monkeypatch, **kw)
    assert json.loads(policy.kanban_json)['capsule'] == {'required': False}
    require_capsule_transport(policy)  # ordinary behaviour unchanged: no refusal


def test_required_capsule_without_transport_fails_closed(tmp_path, monkeypatch):
    from gateway.session_kanban_capsule import require_capsule_transport
    from hermes_state_runtime import RuntimeStoreError
    policy = _policy(tmp_path, monkeypatch, local=False)
    with pytest.raises(RuntimeStoreError, match='capsule_observation_missing'):
        require_capsule_transport(policy)


@pytest.mark.parametrize('frozen', [None, {'required': False}, {'required': 'yes'}])
def test_old_or_replayed_saved_policy_rederives(tmp_path, monkeypatch, frozen):
    """A policy saved before the field existed, or a replay carrying a forged False, still refuses."""
    from gateway.session_kanban_capsule import require_capsule_transport
    from hermes_state_runtime import RuntimeStoreError
    policy = _policy(tmp_path, monkeypatch, local=False)
    context = json.loads(policy.kanban_json)
    if frozen is None:
        context.pop('capsule')
    else:
        context['capsule'] = frozen
    stale = replace(policy, kanban_json=json.dumps(context))
    with pytest.raises(RuntimeStoreError, match='capsule_observation_missing'):
        require_capsule_transport(stale)


def test_unreadable_backend_policy_fails_closed(tmp_path, monkeypatch):
    from gateway.session_kanban_capsule import require_capsule_transport
    from hermes_state_runtime import RuntimeStoreError
    policy = _policy(tmp_path, monkeypatch, local=True)
    (tmp_path / 'config.yaml').unlink()  # the assignee policy can no longer be read
    with pytest.raises(RuntimeStoreError, match='capsule_observation_missing'):
        require_capsule_transport(policy)


def test_no_config_or_param_grant_accepted(tmp_path, monkeypatch):
    """Caller params/config cannot flip the derived requirement (exact param set still enforced)."""
    from gateway.session_kanban import build_kanban_policy
    from hermes_state_runtime import RuntimeStoreError
    connection, params, config = _board(tmp_path, monkeypatch, local=False)
    with pytest.raises(RuntimeStoreError, match='invalid_params'):
        build_kanban_policy(connection, params | {'capsule': {'required': False}}, config)
    policy, _ = build_kanban_policy(connection, params, config | {'capsule': {'required': False}, 'kanban': {'capsule': False}})
    assert json.loads(policy.kanban_json)['capsule']['required'] is True


def test_launch_guard_refuses_before_worker_spawn(tmp_path, monkeypatch):
    """Missing rendezvous at launch: placing_spawn refuses before the managed worker exists."""
    from gateway.session_kanban_scope import placing_spawn
    from hermes_state_runtime import RuntimeStoreError
    policy = _policy(tmp_path, monkeypatch, local=False)
    context = json.loads(policy.kanban_json)
    context.pop('capsule')  # an admission queued by an older owner
    spawned = []
    run = placing_spawn(lambda *a, **k: spawned.append(a), replace(policy, kanban_json=json.dumps(context)))
    with pytest.raises(RuntimeStoreError, match='capsule_observation_missing'):
        run(['python', '-m', 'agent.managed_worker'])
    assert spawned == []


def test_launch_guard_ordinary_card_spawns(tmp_path, monkeypatch):
    from gateway.session_kanban_scope import placing_spawn
    policy = _policy(tmp_path, monkeypatch, local=True)
    monkeypatch.setattr('gateway.session_kanban_scope.resolve_run_scope', lambda p: None)
    spawned = []
    run = placing_spawn(lambda *a, **k: spawned.append(a) or 'proc', policy)
    assert run(['x']) == 'proc' and spawned == [(['x'],)]


async def _run_task_refuses(tmp_path, monkeypatch):
    import gateway.session_kanban as sk
    connection, params, config = _board(tmp_path, monkeypatch, local=False)
    created = []
    monkeypatch.setattr('gateway.run._load_gateway_config', lambda: config)
    monkeypatch.setattr('gateway.session_local.create_local_session', lambda *a, **k: created.append(a))
    connection.authority.db = SimpleNamespace(db_path=str(tmp_path / 'owner.db'), get_session=lambda sid: None)
    await sk.run_task(connection, params)
    return created


def test_admission_refuses_before_session_and_owner_admitted(tmp_path, monkeypatch):
    import asyncio
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from hermes_state_runtime import RuntimeStoreError
    created = []

    async def go():
        created.extend(await _run_task_refuses(tmp_path, monkeypatch))
    with pytest.raises(RuntimeStoreError, match='capsule_observation_missing'):
        asyncio.run(go())
    assert created == []
    with closing(connect(board='owned')) as conn:
        assert not conn.execute("SELECT 1 FROM task_events WHERE kind='owner_admitted'").fetchall()
