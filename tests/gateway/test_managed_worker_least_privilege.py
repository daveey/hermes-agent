"""Synthetic-only regression coverage for managed exec and bootstrap boundaries."""
import json
from types import SimpleNamespace

import pytest

from tests.gateway.test_managed_turn_contract import assignment


@pytest.mark.parametrize('multiplex', [False, True])
def test_exec_environment_is_allowlisted_without_profile_secrets(tmp_path, monkeypatch, multiplex):
    from agent import secret_scope
    from gateway.session_managed_worker import _worker_env
    home = tmp_path / 'profile'
    home.mkdir()
    sentinel = 'synthetic-owner-secret-not-for-child'
    (home / '.env').write_text(f'OPENAI_API_KEY={sentinel}\nOPAQUE_SETTING={sentinel}\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('OPENAI_API_KEY', sentinel)
    monkeypatch.setenv('UNKNOWN_OWNER_CREDENTIAL', sentinel)
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    monkeypatch.setattr(secret_scope, 'is_multiplex_active', lambda: multiplex)
    env = _worker_env(SimpleNamespace(profile_id=str(home)))
    assert isinstance(env, dict), 'exec must never inherit the owner environment implicitly'
    assert sentinel not in json.dumps(env)
    assert env['PATH'] == '/usr/bin:/bin'
    assert env['HERMES_HOME'] == str(home)


def test_logical_profile_keeps_allowlist_without_filesystem_secret_scope(monkeypatch):
    from gateway.session_managed_worker import _worker_env
    from gateway.session_worker_credentials import _profile_values
    sentinel = 'synthetic-logical-owner-secret'
    monkeypatch.setenv('UNKNOWN_OWNER_CREDENTIAL', sentinel)
    monkeypatch.setenv('PYTHONPATH', sentinel)
    monkeypatch.setenv('HERMES_NEMO_RELAY_PLUGINS_TOML', '/synthetic/relay.toml')
    authority = SimpleNamespace(profile_id='owned')
    env = _worker_env(authority)
    assert isinstance(env, dict) and sentinel not in json.dumps(env)
    assert 'HERMES_HOME' not in env
    assert env['HERMES_NEMO_RELAY_PLUGINS_TOML'] == '/synthetic/relay.toml'
    assert _profile_values(authority, SimpleNamespace(ignore_user_config=False)) == {}


def test_bootstrap_contains_no_bound_api_key(tmp_path, monkeypatch):
    from gateway.session_managed_worker import _bootstrap
    _, args = assignment(tmp_path, monkeypatch)
    sentinel = 'synthetic-bound-route-secret'
    monkeypatch.setattr('gateway.session_policy.route_launch_key', lambda *a: sentinel)
    frame = _bootstrap(*args)
    assert sentinel not in json.dumps(frame)
    assert frame.get('api_key') is None


def test_bootstrap_contains_no_inline_config_api_key(tmp_path, monkeypatch):
    from dataclasses import replace
    from gateway.session_managed_worker import _bootstrap
    _, (authority, ref, row, policy, scope) = assignment(tmp_path, monkeypatch)
    config = json.loads(policy.config_json)
    config['model']['api_key'] = 'synthetic-inline-route-secret'
    policy = replace(policy, config_json=json.dumps(config))
    frame = _bootstrap(authority, ref, row, policy, scope)
    assert 'synthetic-inline-route-secret' not in json.dumps(frame)


def test_secret_handle_preserves_launch_and_inline_key_without_bootstrap_material(tmp_path, monkeypatch):
    from dataclasses import replace
    from gateway.session_managed_worker import _bootstrap
    from gateway.session_worker_credentials import hydrate_credentials, resolve_credentials
    _, (authority, ref, row, policy, scope) = assignment(tmp_path, monkeypatch)
    config = json.loads(policy.config_json)
    config['model']['api_key'] = 'synthetic-inline-route-secret'
    policy = replace(policy, config_json=json.dumps(config))
    monkeypatch.setattr('gateway.session_policy.route_launch_key', lambda *a: 'synthetic-launch-secret')
    monkeypatch.setattr('gateway.session_worker_construct.construct_inputs', lambda *a: {
        'fallback_model': {'provider': 'custom', 'api_key': 'synthetic-construct-secret'}})
    frame = _bootstrap(authority, ref, row, policy, scope)
    assert 'synthetic-construct-secret' not in json.dumps(frame)
    assert 'synthetic-launch-secret' not in json.dumps(frame)
    calls = []
    def rpc(method, **params):
        calls.append((method, params))
        return resolve_credentials(authority, params)
    hydrated = hydrate_credentials(frame, rpc)
    assert hydrated['api_key'] == 'synthetic-launch-secret'
    assert json.loads(hydrated['policy']['request_json'])['construct_v1']['fallback_model']['api_key'] == 'synthetic-construct-secret'
    assert json.loads(hydrated['policy']['config_json'])['model']['api_key'] == 'synthetic-inline-route-secret'
    assert calls == [('worker.credentials', scope)]
    assert 'synthetic-inline-route-secret' not in json.dumps(frame), 'resolution cannot mutate the wire frame'
    for field, value in [('epoch', 2), ('generation', 2), ('session_id', 'foreign'), ('secret', 'wrong')]:
        with pytest.raises(Exception, match='permission_denied'):
            resolve_credentials(authority, {**scope, field: value})


def test_route_env_is_positive_and_profile_bound(tmp_path, monkeypatch):
    from gateway.session_worker_credentials import route_environment
    from agent import secret_scope
    from tui_gateway import launch_profile_policy
    monkeypatch.setattr(secret_scope, 'build_profile_secret_scope', lambda home: {
        'ROUTE_KEY': 'synthetic-own', 'UNDECLARED_KEY': 'synthetic-unrelated'})
    monkeypatch.setattr(launch_profile_policy, 'launch_secret_scope', lambda home: {'ROUTE_KEY': 'synthetic-launch'})
    monkeypatch.setattr('tools.environments.local._is_routed_home', lambda home: True)
    config = {'model': {'provider': 'custom:selected'}, 'custom_providers': [
        {'name': 'selected', 'base_url': 'https://selected.invalid', 'key_env': 'ROUTE_KEY'},
        {'name': 'unused', 'base_url': 'https://unused.invalid', 'key_env': 'UNDECLARED_KEY'}]}
    result = route_environment(SimpleNamespace(profile_id=str(tmp_path)),
                               SimpleNamespace(ignore_user_config=False), config)
    assert result == {'ROUTE_KEY': 'synthetic-own'}


@pytest.mark.parametrize('provider, expected', [
    ('openrouter', {'OPENROUTER_API_KEY': 'synthetic-openrouter-key'}),
    ('auto', {'OPENROUTER_API_KEY': 'synthetic-openrouter-key'}),
    ('', {'OPENROUTER_API_KEY': 'synthetic-openrouter-key'}),
    ('custom', {'OPENAI_API_KEY': 'synthetic-openai-key'}),
    ('claude', {'ANTHROPIC_API_KEY': 'synthetic-anthropic-key'}),
])
@pytest.mark.parametrize('auxiliary', [None, {}])
def test_builtin_route_environment_uses_only_selected_provider(tmp_path, monkeypatch, provider, expected, auxiliary):
    from agent import secret_scope
    from gateway.session_worker_credentials import route_environment
    monkeypatch.setattr(secret_scope, 'build_profile_secret_scope', lambda home: {
        'OPENROUTER_API_KEY': 'synthetic-openrouter-key', 'OPENAI_API_KEY': 'synthetic-openai-key',
        'ANTHROPIC_API_KEY': 'synthetic-anthropic-key', 'UNDECLARED_KEY': 'synthetic-unrelated'})
    monkeypatch.setattr('tools.environments.local._is_routed_home', lambda home: True)
    config = {'model': {'provider': provider}, 'auxiliary': auxiliary}
    if provider == 'custom':
        config['model']['base_url'] = 'https://route.invalid'
    result = route_environment(SimpleNamespace(profile_id=str(tmp_path)),
                               SimpleNamespace(ignore_user_config=False), config)
    assert result == expected


@pytest.mark.parametrize('key, expected', [
    ('synthetic-openai-only', {}), ('sk-or-synthetic-legacy', {'OPENAI_API_KEY': 'sk-or-synthetic-legacy'})])
def test_openrouter_legacy_key_is_not_an_unrelated_openai_key(tmp_path, monkeypatch, key, expected):
    from agent import secret_scope
    from gateway.session_worker_credentials import route_environment
    monkeypatch.setattr(secret_scope, 'build_profile_secret_scope', lambda home: {'OPENAI_API_KEY': key})
    monkeypatch.setattr('tools.environments.local._is_routed_home', lambda home: True)
    assert route_environment(SimpleNamespace(profile_id=str(tmp_path)),
                             SimpleNamespace(ignore_user_config=False),
                             {'model': {'provider': 'openrouter'}}) == expected


def test_bootstrap_validator_rejects_raw_key(tmp_path, monkeypatch):
    from agent.managed_worker import validate_bootstrap
    frame, _ = assignment(tmp_path, monkeypatch)
    validate_bootstrap(frame)
    with pytest.raises(ValueError, match='invalid_managed_worker_bootstrap'):
        validate_bootstrap({**frame, 'api_key': 'synthetic-raw-key'})
