"""The task-selected model, not the profile default, authorizes worker keys."""
import json
from contextlib import closing
from types import SimpleNamespace

import pytest

@pytest.fixture
def class_config():
    def route(provider, model, billing):
        return dict(provider=provider, model=model, billing=billing, weight=1,
                    eligible=True, funding_verified=True,
                    capabilities=dict(tools=True, streaming=True, context_tokens=1048576))
    return {'model_routing': {'enabled': True, 'allow_metered': False,
        'enforce_subscription_only': True, 'routes': {
            'carbon': [route('gemini', 'gemini-4-carbon', 'metered')],
            'standard': [route('anthropic', 'standard', 'subscription')],
            'deep': [route('anthropic', 'deep', 'subscription')]}}}


@pytest.mark.parametrize('level,legacy', [('carbon', False), ('standard', False),
                                         ('deep', False), ('carbon', True)])
def test_real_kanban_policy_bootstrap_stages_selected_class(tmp_path, monkeypatch, class_config, level, legacy):
    from gateway.session_contract import SessionActor
    from gateway.session_kanban import build_kanban_policy
    from gateway.session_managed_worker import _bootstrap
    from gateway.session_worker_credentials import resolve_credentials
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_DELEGATED_CHILD', raising=False)
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env', lambda name: str(tmp_path))
    monkeypatch.setattr('hermes_cli.kanban_capabilities._profile_capabilities',
                        lambda *args: {'terminal', 'file', 'git', 'host_repo', 'kanban'})
    monkeypatch.setattr('gateway.session_worker_credentials._profile_values', lambda *args: {
        'GEMINI_API_KEY': 'synthetic-gemini', 'GOOGLE_API_KEY': 'synthetic-google',
        'ANTHROPIC_API_KEY': 'synthetic-anthropic', 'OPENROUTER_API_KEY': 'synthetic-openrouter',
        'UNDECLARED_SECRET': 'synthetic-unrelated'})
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'pyproject.toml').write_text('[project]\nname = "task"\n')
    kb.create_board('owned')
    with closing(connect(board='owned')) as conn:
        tid = kb.create_task(conn, title='Selected class', assignee='default',
                             workspace_kind='dir', workspace_path=str(workspace),
                             model_override='class:' + level, provider_override='auto')
        kb.recompute_ready(conn)
        task = kb.claim_task(conn, tid)
        assert task is not None
    config = class_config
    config.update(auxiliary={}, fallback_model=None, fallback_providers=[])
    # This is the real failed launch shape: no synthetic profile-default replacement.
    config['model'] = ({'model': 'class:standard', 'provider': 'auto'} if legacy else
                       {'default': 'profile-default', 'provider': 'openrouter'})
    profile_model = {**config['model'], 'provider': 'auto'}
    actor = SessionActor('owner', str(tmp_path), frozenset({'session:create'}), 'transport')
    authority = SimpleNamespace(profile_id=str(tmp_path), runner=None)
    connection = SimpleNamespace(authority=authority, actor=actor, native_owner=True)
    policy, private = build_kanban_policy(connection, dict(board='owned', task_id=task.id,
        run_id=task.current_run_id, claim_lock=task.claim_lock), config)
    assert not private
    assert policy.model == 'class:' + level
    assert json.loads(policy.config_json)['model'] == profile_model
    live = SimpleNamespace(source=SimpleNamespace(user_id='u', chat_id='c'), route='kanban-test')
    authority.sessions = {'sid': live}
    ref = SimpleNamespace(session_id='sid')
    scope = dict(profile_id=str(tmp_path), session_id='sid', execution_id='x', generation=1,
                 pid=1, birth=0, secret='synthetic-scope', epoch=1)
    frame = _bootstrap(authority, ref, {'payload': {'text': 'hello', 'finite': True}}, policy, scope)
    assert frame['policy']['model'] == 'class:' + level
    assert json.loads(frame['policy']['config_json'])['model'] == profile_model
    material = resolve_credentials(authority, scope)
    expected = {'GEMINI_API_KEY', 'GOOGLE_API_KEY'} if level == 'carbon' else {'ANTHROPIC_API_KEY'}
    assert set(material['environment']) == expected
    assert all(value not in json.dumps(frame) for value in material['environment'].values())


def test_bootstrap_uses_refreshed_model_not_stale_policy_pin(tmp_path, monkeypatch, class_config):
    from dataclasses import replace
    from gateway.session_managed_worker import _bootstrap
    from gateway.session_worker_credentials import resolve_credentials
    from tests.gateway.test_managed_turn_contract import assignment

    _, (authority, ref, row, policy, scope) = assignment(tmp_path, monkeypatch)
    config = {**json.loads(policy.config_json), **class_config,
              'model': {'default': 'class:standard', 'provider': 'auto'},
              'auxiliary': {}, 'fallback_model': None, 'fallback_providers': []}
    policy = replace(policy, model='class:carbon', config_json=json.dumps(config))
    # A followed profile's effective model can differ from the stored launch pin.
    monkeypatch.setattr(type(policy), 'effective_selection',
                        lambda *args, **kwargs: (config, 'class:standard', False))
    monkeypatch.setattr('gateway.session_worker_credentials._profile_values', lambda *args: {
        'GEMINI_API_KEY': 'synthetic-gemini', 'ANTHROPIC_API_KEY': 'synthetic-anthropic'})
    frame = _bootstrap(authority, ref, row, policy, scope)
    assert frame['policy']['model'] == 'class:standard'
    assert resolve_credentials(authority, scope)['environment'] == {
        'ANTHROPIC_API_KEY': 'synthetic-anthropic'}


def test_primary_override_preserves_declared_auxiliary_inheritance(tmp_path, monkeypatch, class_config):
    from gateway.session_worker_credentials import route_environment

    class_config.update(model={'default': 'class:standard', 'provider': 'auto'},
                        auxiliary={'title': {}})
    monkeypatch.setattr('gateway.session_worker_credentials._profile_values', lambda *args: {
        'GEMINI_API_KEY': 'synthetic-gemini', 'ANTHROPIC_API_KEY': 'synthetic-anthropic'})
    assert route_environment(SimpleNamespace(profile_id=str(tmp_path)),
        SimpleNamespace(ignore_user_config=False), class_config, model='class:carbon') == {
            'GEMINI_API_KEY': 'synthetic-gemini', 'ANTHROPIC_API_KEY': 'synthetic-anthropic'}
