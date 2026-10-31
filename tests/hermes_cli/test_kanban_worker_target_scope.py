"""Frozen worker explicit-target readers retain secret and managed scopes."""
import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('managed', [False, True])
def test_target_scope_under_frozen_policy(tmp_path, managed):
    home = tmp_path / '.hermes'
    target = home / 'profiles' / 'target'
    target.mkdir(parents=True)
    (home / 'config.yaml').write_text('{}')
    (target / '.env').write_text('POLICY_PROVIDER=anthropic\nPOLICY_MARKER=target\n')
    config = {'model': {'provider': '${POLICY_PROVIDER}'},
              'kanban': {'pr_acceptance': {'gh_command_prefix': [sys.executable, '${POLICY_MARKER}']}}}
    (target / 'config.yaml').write_text(json.dumps(config))
    overlay = tmp_path / 'managed'
    overlay.mkdir()
    if managed:
        (overlay / 'config.yaml').write_text(json.dumps({
            'model': {'provider': 'custom:managed'},
            'kanban': {'pr_acceptance': {'gh_command_prefix': [sys.executable, 'managed']}}}))
    script = tmp_path / 'scope.py'
    script.write_text(f'import sys; sys.path.insert(0, {str(ROOT)!r})\n' + textwrap.dedent(f'''
        from agent.safe_worker_policy import _bind_worker_config, worker_config_snapshot
        snapshot = {{'model': {{'provider': 'openai-codex'}}}}
        _bind_worker_config(snapshot)
        from agent.secret_scope import set_secret_scope, current_secret_scope
        from hermes_constants import get_hermes_home
        from hermes_cli.kanban_model_assignment import _assignee_config
        from hermes_cli.kanban_pin_provider import resolve_pin_provider
        from hermes_cli.kanban_pr_acceptance import _gh_command_prefix
        caller_scope = {{'POLICY_PROVIDER': 'openai-codex', 'POLICY_MARKER': 'caller'}}
        set_secret_scope(caller_scope)
        home = get_hermes_home()
        provider = {'custom:managed' if managed else 'anthropic'!r}
        marker = {'managed' if managed else 'target'!r}
        assert _assignee_config('target')['model']['provider'] == provider
        model = {'private-fixture-model' if managed else 'claude-test'!r}
        def credentials(p):
            assert get_hermes_home() == home / 'profiles' / 'target'
            assert current_secret_scope()['POLICY_MARKER'] == 'target'
            return p == provider
        assert resolve_pin_provider(model, None, assignee='target', has_credentials=credentials) == (model, provider)
        assert _gh_command_prefix({str(target)!r}) == [sys.executable, marker]
        assert get_hermes_home() == home
        assert current_secret_scope() == caller_scope
        assert worker_config_snapshot() == snapshot
        print('SCOPES_OK')
    '''))
    from tests.gateway.fixtures.local_recovery_probe import child_env
    env = child_env()
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), HERMES_HOME=str(home),
               HERMES_MANAGED_DIR=str(overlay), HERMES_DISABLE_LAZY_INSTALLS='1')
    proc = subprocess.run([sys.executable, str(script)], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=90, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'SCOPES_OK' in proc.stdout
