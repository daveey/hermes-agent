"""Synthetic exec compatibility controls; no operator credentials are read."""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.gateway.test_managed_turn_contract import assignment


@pytest.mark.parametrize('names', [
    ['HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'NODE_EXTRA_CA_CERTS'],
    ['USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'HOMEDRIVE', 'HOMEPATH'],
    ['SSH_AUTH_SOCK', 'DISPLAY', 'XDG_CONFIG_HOME', 'XDG_DATA_DIRS', 'LD_LIBRARY_PATH', 'TERM',
     'KUBECONFIG', 'HERMES_BUNDLED_SKILLS'],
])
def test_exec_preserves_explicit_operational_settings(tmp_path, monkeypatch, names):
    from gateway.session_managed_worker import _worker_env
    for name in names:
        monkeypatch.setenv(name, '/synthetic/' + name.lower())
    monkeypatch.setenv('UNKNOWN_OWNER_CREDENTIAL', 'synthetic-secret-excluded')
    env = _worker_env(SimpleNamespace(profile_id=str(tmp_path)))
    assert {name: env.get(name) for name in names} == {name: os.environ[name] for name in names}
    assert 'synthetic-secret-excluded' not in json.dumps(env)


@pytest.mark.parametrize('cooldown', ['0', '17'])
def test_exec_preserves_board_cooldown_without_task_identity(tmp_path, monkeypatch, cooldown):
    from gateway.session_managed_worker import _worker_env
    name = 'HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS'
    monkeypatch.setenv(name, cooldown)
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'synthetic-task')
    monkeypatch.setenv('HERMES_KANBAN_UNKNOWN_SECRET', 'synthetic-secret')
    env = _worker_env(SimpleNamespace(profile_id=str(tmp_path)))
    assert env.get(name) == cooldown
    assert 'HERMES_KANBAN_TASK' not in env
    assert 'HERMES_KANBAN_UNKNOWN_SECRET' not in env


@pytest.mark.parametrize('desktop_owner', [False, True])
def test_desktop_completion_owner_crosses_exec_without_backend_token(tmp_path, monkeypatch, desktop_owner):
    from gateway.session_managed_worker import _bootstrap, _worker_env
    from hermes_cli import process_identity
    _, args = assignment(tmp_path, monkeypatch)
    monkeypatch.setattr(process_identity, 'is_desktop_owned_backend', lambda: desktop_owner)
    monkeypatch.setenv('HERMES_DASHBOARD_SESSION_TOKEN', 'synthetic-desktop-token')
    frame = _bootstrap(*args)
    env = _worker_env(args[0])
    assert 'synthetic-desktop-token' not in json.dumps([frame, env])
    code = '''
import json, sys
from gateway import session_worker_credentials
from agent import runtime_session_store
from tools.process_registry_obligations import owes_tui_delivery
from hermes_cli.process_identity import is_desktop_owned_backend
from types import SimpleNamespace
runtime_session_store._worker_process = True
getattr(session_worker_credentials, 'bind_completion_owner', lambda frame: None)(json.load(sys.stdin))
session = SimpleNamespace(notify_on_complete=True, session_key='synthetic-chat', watcher_platform=None)
expected = json.loads(sys.argv[1])
assert owes_tui_delivery(session) is expected, 'managed completion keeper destination mismatch'
assert not is_desktop_owned_backend(), 'completion ownership must not grant backend identity'
'''
    result = subprocess.run([sys.executable, '-c', code, json.dumps(desktop_owner)], input=json.dumps(frame), env=env,
                            text=True, capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('routed', [False, True])
def test_declared_tool_credentials_are_owner_hydrated_not_bootstrapped(tmp_path, monkeypatch, routed):
    from dataclasses import replace
    from agent import secret_scope
    from gateway.session_managed_worker import _bootstrap, _worker_env
    from gateway.session_worker_credentials import hydrate_credentials, resolve_credentials
    from tools.registry import registry
    import toolsets
    _, (authority, ref, row, policy, scope) = assignment(tmp_path, monkeypatch)
    values = {'FIRECRAWL_API_KEY': 'synthetic-declared-tool-key', 'GH_TOKEN': 'synthetic-undeclared-key'}
    config = json.loads(policy.config_json)
    config['platform_toolsets'] = {'cli': ['web']}
    policy = replace(policy, ignore_user_config=False, config_json=json.dumps(config))
    monkeypatch.setattr('tools.environments.local._is_routed_home', lambda home: routed)
    monkeypatch.setattr('tui_gateway.launch_profile_policy.launch_secret_scope', lambda home: values)
    own = {'FIRECRAWL_API_KEY': 'synthetic-routed-tool-key'} if routed else {}
    monkeypatch.setattr(secret_scope, 'build_profile_secret_scope', lambda home: own)
    monkeypatch.setattr(type(policy), 'current_toolsets', lambda *args: ('web',))
    monkeypatch.setattr(toolsets, 'resolve_toolset', lambda name: ['web_extract'])
    monkeypatch.setattr(registry, 'get_all_entries', lambda: [
        SimpleNamespace(name='web_extract', requires_env=['FIRECRAWL_API_KEY']),
        SimpleNamespace(name='unselected', requires_env=['GH_TOKEN'])])
    frame = _bootstrap(authority, ref, row, policy, scope)
    assert all(value not in json.dumps([frame, _worker_env(authority)]) for value in values.values())
    from hermes_state_runtime import RuntimeStoreError
    try:
        material = resolve_credentials(authority, scope)
    except RuntimeStoreError:
        material = {'environment': {}}
    expected = own or {'FIRECRAWL_API_KEY': values['FIRECRAWL_API_KEY']}
    assert material['environment'] == expected
    monkeypatch.setenv('FIRECRAWL_API_KEY', 'synthetic-unrelated-ambient')
    hydrate_credentials(frame, lambda method, **params: resolve_credentials(authority, params))
    assert os.environ['FIRECRAWL_API_KEY'] == expected['FIRECRAWL_API_KEY']
