"""Explicit target inspection must not reuse a caller's frozen authority."""
import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("entry", ["model", "provider", "pr"])
@pytest.mark.parametrize("broken", [False, True])
def test_frozen_worker_target_policy(tmp_path, entry, broken):
    home = tmp_path / ".hermes"
    target = home / "profiles" / "target"
    target.mkdir(parents=True)
    (home / "config.yaml").write_text('{}')
    target_config = {
        "model": {"provider": "anthropic"},
        "kanban": {"pr_acceptance": {"gh_command_prefix": [sys.executable, "target"]}},
    }
    (target / "config.yaml").write_text('model: [broken' if broken else json.dumps(target_config))
    snapshot = {
        "model": {"provider": "openai-codex"},
        "kanban": {"pr_acceptance": {"gh_command_prefix": [sys.executable, "caller"]}},
    }
    script = tmp_path / "probe.py"
    script.write_text(f"import sys; sys.path.insert(0, {str(ROOT)!r})\n" + textwrap.dedent(f"""
        from pathlib import Path
        from agent.safe_worker_policy import _bind_worker_config, worker_config_snapshot
        snapshot = {snapshot!r}
        _bind_worker_config(snapshot)
        from hermes_constants import get_hermes_home
        from hermes_cli.config_effective import load_user_config_effective
        from hermes_cli.kanban_model_assignment import _assignee_config, validate_model_assignment
        from hermes_cli.kanban_pin_provider import _configured_provider, resolve_pin_provider
        from hermes_cli.kanban_pr_acceptance import _gh_command_prefix, _GateSetupError
        from ruamel.yaml.error import YAMLError
        target = Path({str(target)!r})
        from agent.secret_scope import current_secret_scope, set_secret_scope
        caller_scope = {{'TARGET_POLICY_MARKER': 'caller'}}
        set_secret_scope(caller_scope)
        before_home = get_hermes_home()
        entry, broken = {entry!r}, {broken!r}
        if entry == 'model':
            if broken:
                try:
                    _assignee_config('target')
                except YAMLError:
                    pass
                else:
                    raise AssertionError('invalid target policy was accepted')
            else:
                assert _assignee_config('target')['model']['provider'] == 'anthropic'
                validate_model_assignment('target', 'claude-test', None)
            assert _assignee_config(None)['model']['provider'] == 'openai-codex'
        elif entry == 'provider':
            assert _configured_provider(target) == ('' if broken else 'anthropic')
            if not broken:
                assert resolve_pin_provider('claude-test', None, assignee='target',
                                            has_credentials=lambda p: p == 'anthropic') == ('claude-test', 'anthropic')
            assert _configured_provider(None) == 'openai-codex'
        else:
            if broken:
                try:
                    _gh_command_prefix(str(target))
                except _GateSetupError:
                    pass
                else:
                    raise AssertionError('invalid PR policy was accepted')
            else:
                assert _gh_command_prefix(str(target)) == [sys.executable, 'target']
            assert _gh_command_prefix(None) == [sys.executable, 'caller']
        assert get_hermes_home() == before_home
        assert current_secret_scope() == caller_scope
        assert worker_config_snapshot() == snapshot
        assert load_user_config_effective()['model']['provider'] == 'openai-codex'
        assert not list(target.rglob('backups/config'))
        print('TARGET_POLICY_OK')
    """))
    from tests.gateway.fixtures.local_recovery_probe import child_env
    env = child_env()
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), HERMES_HOME=str(home),
               HERMES_DISABLE_LAZY_INSTALLS="1")
    proc = subprocess.run([sys.executable, str(script)], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=90, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'TARGET_POLICY_OK' in proc.stdout
