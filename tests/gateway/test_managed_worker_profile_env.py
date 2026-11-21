"""A served secondary profile's managed worker never inherits launch-profile residue."""
import os
from types import SimpleNamespace

from agent.secret_scope import set_multiplex_active


def test_secondary_worker_env_drops_launch_terminal_policy_and_env_settings(tmp_path, monkeypatch):
    """Same contract as the cron and kanban dispatchers: a worker for served profile X gets the
    env a standalone ``hermes -p X`` gateway would build — X's HERMES_HOME and X's ``.env`` over a
    base without the launch profile's ``TERMINAL_*`` policy or launch-only ``.env`` settings."""
    from gateway.session_managed_worker import _worker_env
    # Own the terminal inputs independently of the invoking worker shell.
    for key in list(os.environ):
        if key.upper().startswith('TERMINAL_'):
            monkeypatch.delenv(key)
    root = tmp_path / '.hermes'
    beta = root / 'profiles' / 'beta'
    beta.mkdir(parents=True)
    (root / '.env').write_text('HERMES_MODEL=launch-model\nTERMINAL_ENV=docker\n')
    (beta / '.env').write_text('BETA_ONLY_TOKEN=beta-secret\n')
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setenv('HERMES_MODEL', 'launch-model')
    monkeypatch.setenv('TERMINAL_ENV', 'docker')
    monkeypatch.setenv('TERMINAL_CWD', str(root))
    # Include launch bridge policy, not only the fixture's backend and cwd.
    monkeypatch.setenv('TERMINAL_DOCKER_IMAGE_PINNED', '1')
    monkeypatch.setenv('TERMINAL_HOME_MODE', 'profile')
    set_multiplex_active(True)
    try:
        env = _worker_env(SimpleNamespace(profile_id=str(beta)))
    finally:
        set_multiplex_active(False)
    assert env['HERMES_HOME'] == str(beta)
    assert 'BETA_ONLY_TOKEN' not in env
    assert not {k for k in env if k.startswith('TERMINAL_')}, env
    assert 'HERMES_MODEL' not in env


def test_derived_terminal_policy_preserves_launch_and_managed_values(tmp_path, monkeypatch):
    from hermes_cli import env_loader
    from tools.environments.local import strip_launch_profile_env

    root = tmp_path / '.hermes'
    root.mkdir()
    beta = root / 'profiles' / 'beta'
    beta.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(root))
    values = {'TERMINAL_DOCKER_IMAGE_PINNED': '1', 'TERMINAL_HOME_MODE': 'profile'}
    monkeypatch.setattr(env_loader, 'managed_dotenv_keys', lambda: set())
    assert strip_launch_profile_env(values.copy(), root) == values
    assert strip_launch_profile_env(values.copy(), beta) == {}
    assert strip_launch_profile_env({k.lower(): v for k, v in values.items()}, beta) == {}
    monkeypatch.setattr(env_loader, 'managed_dotenv_keys', lambda: set(values))
    assert strip_launch_profile_env(values.copy(), beta) == values


def test_target_home_mode_is_rebuilt_by_both_terminal_bridges(tmp_path, monkeypatch):
    from hermes_cli.config import apply_terminal_config_to_env
    from tools.terminal_scope import build_profile_terminal_scope

    target = tmp_path / 'target'
    target.mkdir()
    (target / 'config.yaml').write_text('terminal:\n  home_mode: profile\n')
    monkeypatch.setenv('HERMES_HOME', str(target))
    monkeypatch.setenv('TERMINAL_HOME_MODE', 'real')
    scope = build_profile_terminal_scope(target)
    assert scope['TERMINAL_HOME_MODE'] == 'profile'
    child_env = apply_terminal_config_to_env(env={}, config={'terminal': {'home_mode': 'profile'}})
    assert child_env['TERMINAL_HOME_MODE'] == 'profile'


def test_launch_scrub_runs_before_the_turn_context_is_injected(tmp_path, monkeypatch):
    """The launch residue strip runs on the RAW environ, before ``build_subprocess_env`` bridges this
    turn's own session context: a launch ``.env`` that names ``HERMES_SESSION_ID`` must not erase the
    id derived for the secondary's turn (#109338 ordering)."""
    from gateway.run import _profile_runtime_scope
    from gateway.session_context import scoped_current_session_id
    from gateway.session_managed_worker import _worker_env
    root = tmp_path / '.hermes'
    beta = root / 'profiles' / 'beta'
    beta.mkdir(parents=True)
    (root / '.env').write_text('HERMES_SESSION_ID=launch-session\nLAUNCH_ONLY=launch-value\n')
    (beta / '.env').write_text('BETA_ONLY_TOKEN=beta-secret\n')
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setenv('HERMES_SESSION_ID', 'launch-session')
    monkeypatch.setenv('LAUNCH_ONLY', 'launch-value')
    before = dict(os.environ)
    set_multiplex_active(True)
    try:
        with _profile_runtime_scope(beta), scoped_current_session_id('owned-session'):
            env = _worker_env(SimpleNamespace(profile_id=str(beta)))
    finally:
        set_multiplex_active(False)
    assert env['HERMES_SESSION_ID'] == 'owned-session'
    assert 'LAUNCH_ONLY' not in env
    assert 'BETA_ONLY_TOKEN' not in env
    assert dict(os.environ) == before
