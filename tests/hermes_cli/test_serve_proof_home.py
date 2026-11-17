"""Proof backends must select disposable state before any config consumer.

The subprocess tripwire stops old code at its first config import, so RED never
starts consumers (even against the disposable sentinel). No production signals:
every home is a temporary directory and every launched process is owned here.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import pytest

from tools.environments.local import build_subprocess_env

ROOT = Path(__file__).resolve().parents[2]
_ORIGINS = ("HERMES_AGENT_TOOL_CALL", "HERMES_AGENT_ORIGIN", "HERMES_KANBAN_TASK",
            "HERMES_KANBAN_WORKER_TASK", "HERMES_DELEGATED_CHILD_CONTEXT")
_BOUNDARY = '''
import importlib.abc, os, runpy, sys
class ConfigBoundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'hermes_cli.config':
            print('CONFIG_HOME=' + os.environ.get('HERMES_HOME', ''), flush=True)
            raise SystemExit(87)
sys.meta_path.insert(0, ConfigBoundary())
sys.argv = ['hermes'] + sys.argv[1:]
runpy.run_module('hermes_cli.main', run_name='__main__')
'''


@pytest.fixture
def sentinel(tmp_path):
    return make_sentinel(tmp_path)


def make_sentinel(tmp_path):
    """A production-shaped home (it IS ``$HOME/.hermes`` for the launched processes)."""
    from hermes_state import SessionDB

    home = tmp_path / '.hermes'
    home.mkdir()
    db = SessionDB(home / 'state.db')
    db.create_session('unrelated-desktop', source='desktop')
    db.close()
    # An actually exited owned child, not a guessed PID that might be live.
    dead = subprocess.Popen([sys.executable, '-c', 'pass'], env=build_subprocess_env())
    assert dead.wait(timeout=5) == 0
    marker = home / 'desktop' / 'interrupted_turns.json'
    marker.parent.mkdir()
    marker.write_text(json.dumps({'unrelated-desktop': {
        'prompt': 'unrelated recoverable fixture', 'started_at': time.time(),
        'attempts': 0, 'auto_continue': True, 'writer_pid': dead.pid,
    }}))
    (home / 'processes.json').write_text(json.dumps([{
        'session_id': 'proc_unrelated', 'session_key': 'unrelated-desktop', 'pid': dead.pid,
        'persist_on_release': True, 'notify_on_complete': True,
    }]))
    return home


def _snapshot(home):
    return {str(p.relative_to(home)): p.read_bytes() for p in home.rglob('*') if p.is_file()}


def _clean_env(sentinel, origin=None, extra=None):
    env = build_subprocess_env(base={'PATH': os.environ['PATH']}, inherit_profile_home=False,
                               scrub_secrets=False)
    # Isolation is explicit: never inherit account, routing, launcher or worker state.
    env = {k: v for k, v in env.items() if not k.startswith('HERMES_')}
    env.update(HOME=str(sentinel.parent), HERMES_HOME=str(sentinel),
               TMPDIR=str(sentinel.parent), PYTHONPATH=str(ROOT))
    if origin:
        env[origin] = '1'
    env.update(extra or {})
    return env


def _launch(sentinel, args, origin=None, *, extra=None, wrapper=None, env_override=None):
    env = env_override if env_override is not None else _clean_env(sentinel, origin, extra)
    command = [sys.executable, '-c', _BOUNDARY, *args]
    if wrapper == 'python':
        command = [sys.executable, '-c',
                   'import subprocess,sys; raise SystemExit(subprocess.call(sys.argv[1:]))', *command]
    elif wrapper == 'remote':
        # What the SSH terminal backend runs on the far side: sshd's clean env + the origin prefix.
        from hermes_cli.proof_home import remote_origin_prefix

        env = {k: v for k, v in env.items() if k not in _ORIGINS}
        command = ['bash', '-c', remote_origin_prefix({'HERMES_AGENT_ORIGIN': 'tool'})
                   + 'exec ' + shlex.join(command)]
    return subprocess.run(command, env=env, cwd=ROOT, capture_output=True,
                          text=True, encoding='utf-8', timeout=10, check=False)


def _assert_refused(result, sentinel, before):
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert '--proof-home' in result.stderr
    assert 'CONFIG_HOME=' not in result.stdout
    assert _snapshot(sentinel) == before


@pytest.mark.parametrize('origin', _ORIGINS)
@pytest.mark.parametrize('command', ['serve', 'dashboard'])
def test_agent_production_home_rejected_before_config(sentinel, origin, command):
    before = _snapshot(sentinel)
    started = time.monotonic()
    result = _launch(sentinel, [command, '--isolated', '--port', '0'], origin)
    _assert_refused(result, sentinel, before)
    assert time.monotonic() - started < 10


@pytest.mark.parametrize('wrapper', ['python', 'remote'])
def test_wrapped_tool_origin_rejected_without_worker_vars(sentinel, wrapper):
    before = _snapshot(sentinel)
    origin = 'HERMES_AGENT_TOOL_CALL' if wrapper == 'python' else None
    result = _launch(sentinel, ['serve', '--isolated'], origin, wrapper=wrapper)
    _assert_refused(result, sentinel, before)


@pytest.mark.parametrize('extra,args', [
    ({'HERMES_DESKTOP': '1', 'HERMES_DASHBOARD_SESSION_TOKEN': 'fixture-only'}, ['serve']),
    ({'HERMES_DESKTOP': '1'}, ['serve', '--ssh-session-token-file', '/fixture/token']),
    # argparse's equals form names the same Desktop token-file flag.
    ({'HERMES_DESKTOP': '1'}, ['serve', '--ssh-session-token-file=/fixture/token']),
])
def test_desktop_backend_shape_without_isolated_still_rejected(sentinel, extra, args):
    before = _snapshot(sentinel)
    result = _launch(sentinel, args, 'HERMES_AGENT_TOOL_CALL', extra=extra)
    _assert_refused(result, sentinel, before)


_PARSE_BOUNDARY = '''
import argparse, runpy, sys
_parse = argparse.ArgumentParser.parse_known_args
def parse_known_args(self, *a, **k):
    ns, rest = _parse(self, *a, **k)
    if getattr(ns, 'ssh_session_token_file', None):
        print('PARSED_TOKEN_FILE=' + ns.ssh_session_token_file, flush=True)
        raise SystemExit(87)
    return ns, rest
argparse.ArgumentParser.parse_known_args = parse_known_args
sys.argv = ['hermes'] + sys.argv[1:]
runpy.run_module('hermes_cli.main', run_name='__main__')
'''


@pytest.mark.parametrize('args', [
    ['serve', '--ssh-session-tok=/fixture/token'],
    ['serve', '--ssh-session', '/fixture/token'],
])
def test_abbreviated_token_file_flag_is_rejected_by_parser(sentinel, args):
    """The bootstrap shape check matches the exact flag, so no parser may accept a prefix of it: an
    agent-stamped abbreviated form would otherwise set the token file while skipping the guard.
    (``hermes_cli.main`` imports config at module level, so the parser rejects it after that import;
    the tripwire here is the first parse that yields a token file, before ``cmd_dashboard``.)"""
    before = _snapshot(sentinel)
    # Unlike the config-tripwire launches above, this one runs main's real boot. Its default-plugin
    # step would start a detached installer that fetches the live catalog and publishes it into the
    # sentinel's cache AFTER this subprocess returns (an atomic temp + rename that outlives the test).
    # That install is unrelated to the parser contract, so the fixture opts out of lazy installs.
    env = _clean_env(sentinel, 'HERMES_AGENT_TOOL_CALL',
                     {'HERMES_DESKTOP': '1', 'HERMES_DISABLE_LAZY_INSTALLS': '1'})
    result = subprocess.run([sys.executable, '-c', _PARSE_BOUNDARY, *args], env=env, cwd=ROOT,
                            capture_output=True, text=True, encoding='utf-8', timeout=30, check=False)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert 'PARSED_TOKEN_FILE=' not in result.stdout
    assert 'unrecognized arguments' in result.stderr
    # Tripwire: spawn_install marks its request before it forks, so a detached installer is visible.
    assert not (sentinel / 'cache' / 'default-plugins.rerun').exists()
    # main's startup seeds default home files for every command; the recovery state is untouched.
    # Read exactly the recovery files (a missing one reads as None and fails): enumerating the whole
    # home would also read other writers' transient files, which the contract says nothing about.
    after = {name: (sentinel / name).read_bytes() if (sentinel / name).exists() else None
             for name in before}
    assert after == before


@pytest.mark.parametrize('args', [['dashboard', '--port', '0', '--no-open'], ['serve', '--port', '0']])
def test_desktop_chat_tool_child_may_run_plain_backend(sentinel, args):
    """A Desktop chat's tool children inherit HERMES_DESKTOP=1 (and never the session token):
    that alone is not a Desktop-owned backend, so a plain production dashboard still starts."""
    result = _launch(sentinel, args, 'HERMES_AGENT_TOOL_CALL', extra={'HERMES_DESKTOP': '1'})
    assert result.returncode == 87, (result.stdout, result.stderr)
    assert 'Refused proof backend' not in result.stderr


def test_profile_alias_of_production_rejected(sentinel, tmp_path):
    (sentinel / 'profiles' / 'work').mkdir(parents=True)
    alias = tmp_path / 'work-alias'
    alias.symlink_to(sentinel / 'profiles' / 'work', target_is_directory=True)
    before = _snapshot(sentinel)
    result = _launch(sentinel, ['serve', '--isolated'], 'HERMES_KANBAN_TASK', extra={'HERMES_HOME': str(alias)})
    _assert_refused(result, sentinel, before)


@pytest.mark.parametrize('surface', ['terminal', 'process', 'execute_code'])
def test_tool_child_stamp_without_worker_vars(monkeypatch, sentinel, surface):
    from tools.environments import local
    from tools.code_execution_env import _scrub_child_env

    for key in _ORIGINS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HERMES_HOME', str(sentinel))
    surfaces = {
        'terminal': lambda: local._scrubbed_env([({'PATH': os.environ['PATH']}, False)], frozenset(), lambda p: p),
        'process': lambda: local._sanitize_subprocess_env({'PATH': os.environ['PATH']}),
        'execute_code': lambda: _scrub_child_env({'PATH': os.environ['PATH']}),
    }
    result = surfaces[surface]()
    assert result.get('HERMES_AGENT_TOOL_CALL') == '1'
    assert result.get('HERMES_AGENT_ORIGIN')
    assert result.get('HERMES_AGENT_HOME') == str(sentinel)


def test_ssh_backend_reexports_origin_remotely(monkeypatch):
    from tools.environments import ssh

    seen = {}
    env = ssh.SSHEnvironment.__new__(ssh.SSHEnvironment)
    monkeypatch.setattr(env, '_build_ssh_command', lambda send_env=None: ['ssh', 'host'], raising=False)
    monkeypatch.setattr(ssh, '_popen_bash', lambda cmd, stdin, env=None: seen.setdefault('cmd', cmd))
    monkeypatch.setattr(ssh, 'resolve_passthrough_env', lambda **_: ({}, set()))
    monkeypatch.delenv('HERMES_AGENT_ORIGIN', raising=False)
    env._run_bash('hermes serve --isolated')
    remote = seen['cmd'][-1]
    assert 'HERMES_AGENT_TOOL_CALL=1' in remote and 'HERMES_AGENT_ORIGIN=tool' in remote
    assert remote.index('HERMES_AGENT_TOOL_CALL') < remote.index('hermes serve')


@pytest.mark.parametrize('selection', ['flag', 'env'])
def test_explicit_proof_home_selected_before_config(sentinel, tmp_path, selection):
    proof = tmp_path / 'proof'
    proof.mkdir()
    before = _snapshot(sentinel)
    args = ['serve', '--isolated']
    extra = {'HERMES_AGENT_HOME': str(sentinel)}
    if selection == 'flag':
        args += ['--proof-home', str(proof)]
    else:
        extra['HERMES_PROOF_HOME'] = str(proof)
    result = _launch(sentinel, args, 'HERMES_AGENT_TOOL_CALL', extra=extra)
    assert result.returncode == 87, (result.stdout, result.stderr)
    assert 'CONFIG_HOME=' + str(proof) in result.stdout
    assert _snapshot(sentinel) == before


def _invalid_target(kind, sentinel, tmp_path, extra):
    if kind == 'alias':
        target = tmp_path / 'alias'
        target.symlink_to(sentinel, target_is_directory=True)
        return target
    if kind in ('profile', 'profile_alias'):
        target = sentinel / 'profiles' / 'work'
        if kind == 'profile':
            target.mkdir(parents=True)
            return target
        outside = tmp_path / 'outside-profile'
        outside.mkdir()
        target.parent.mkdir(parents=True)
        target.symlink_to(outside, target_is_directory=True)
        return outside
    if kind == 'agent':
        target = tmp_path / 'custom-agent'
        target.mkdir()
        extra['HERMES_AGENT_HOME'] = str(target)
        return target
    if kind == 'file':
        (tmp_path / 'file').touch()
    return {'default': sentinel, 'missing': tmp_path / 'missing', 'file': tmp_path / 'file'}[kind]


@pytest.mark.parametrize('kind', ['default', 'alias', 'profile', 'profile_alias', 'agent', 'missing', 'file'])
def test_invalid_proof_home_is_read_only(sentinel, tmp_path, kind):
    extra = {}
    target = _invalid_target(kind, sentinel, tmp_path, extra)
    before = _snapshot(sentinel)
    result = _launch(sentinel, ['serve', '--proof-home', str(target)], extra=extra)
    _assert_refused(result, sentinel, before)


def test_proof_home_with_profile_flag_rejected(sentinel, tmp_path):
    proof = tmp_path / 'proof'
    proof.mkdir()
    before = _snapshot(sentinel)
    result = _launch(sentinel, ['-p', 'work', 'serve', '--proof-home', str(proof)])
    _assert_refused(result, sentinel, before)


@pytest.mark.parametrize('extra,args', [
    ({}, ['serve', '--isolated']),
    ({'HERMES_DESKTOP': '1', 'HERMES_DASHBOARD_SESSION_TOKEN': 'fixture-only'}, ['serve', '--isolated']),
    ({'HERMES_DESKTOP': '1'}, ['serve', '--isolated', '--ssh-session-token-file', '/fixture/token']),
    ({'HERMES_DESKTOP': '1', 'HERMES_ALWAYS_ON_DASHBOARD': '1'},
     ['serve', '--isolated', '--ssh-session-token-file', '/fixture/token']),
    # An agent may still run a plain production dashboard; only restart-proof shapes are refused.
    ({'HERMES_KANBAN_TASK': 't_fixture'}, ['dashboard', '--port', '0']),
    ({'HERMES_AGENT_TOOL_CALL': '1'}, ['serve', '--isolated', '--status']),
])
def test_existing_backend_contracts_reach_config(sentinel, extra, args):
    result = _launch(sentinel, args, extra=extra)
    assert result.returncode == 87, (result.stdout, result.stderr)
    assert 'Refused proof backend' not in result.stderr


def test_web_server_recheck_refuses_unselected_proof_home(monkeypatch, sentinel, tmp_path):
    from hermes_cli.proof_home import assert_backend_home

    proof = tmp_path / 'proof'
    proof.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(sentinel))
    monkeypatch.delenv('HERMES_PROOF_HOME', raising=False)
    with pytest.raises(SystemExit) as exc:
        assert_backend_home(isolated=True, argv=['serve', '--proof-home', str(proof)])
    assert exc.value.code == 2
    monkeypatch.setenv('HERMES_HOME', str(proof))
    monkeypatch.setenv('HERMES_PROOF_HOME', str(proof))
    assert_backend_home(isolated=True, argv=['serve', '--proof-home', str(proof)])


def test_recovery_refuses_production_before_config(monkeypatch, sentinel):
    from tui_gateway import startup_resume
    from hermes_cli import config

    monkeypatch.setenv('HERMES_AGENT_TOOL_CALL', '1')
    monkeypatch.setenv('HERMES_AGENT_HOME', str(sentinel))
    monkeypatch.setenv('HERMES_HOME', str(sentinel))
    monkeypatch.setattr(config, 'load_config', lambda: pytest.fail('production config was loaded'))
    assert startup_resume.start_startup_resume_sweep() is None


def test_readopt_refuses_production_jobs(monkeypatch, sentinel):
    from tools import process_registry_readopt as readopt

    class Registry:
        def recover_from_checkpoint(self, owner=None):
            pytest.fail('production processes.json was recovered')

    monkeypatch.setenv('HERMES_DESKTOP', '1')
    monkeypatch.setenv('HERMES_DASHBOARD_SESSION_TOKEN', 'fixture-only')
    monkeypatch.setenv('HERMES_KANBAN_WORKER_TASK', 't_fixture')
    monkeypatch.setenv('HERMES_AGENT_HOME', str(sentinel))
    monkeypatch.setenv('HERMES_HOME', str(sentinel))
    before = _snapshot(sentinel)
    assert readopt.recover_tui_orphaned_processes(Registry()) == 0
    assert _snapshot(sentinel) == before


def test_disposable_home_recovery_is_not_globally_disabled(monkeypatch, sentinel, tmp_path):
    from hermes_cli.proof_home import recovery_home_allowed

    home = tmp_path / 'proof'
    home.mkdir()
    monkeypatch.setenv('HERMES_AGENT_TOOL_CALL', '1')
    monkeypatch.setenv('HERMES_AGENT_HOME', str(sentinel))
    monkeypatch.setenv('HERMES_PROOF_HOME', str(home))
    assert recovery_home_allowed(home)
    assert not recovery_home_allowed(sentinel)
    monkeypatch.delenv('HERMES_PROOF_HOME')
    for key in _ORIGINS + ('HERMES_AGENT_HOME',):
        monkeypatch.delenv(key, raising=False)
    assert recovery_home_allowed(sentinel)  # production backends keep their own recovery


_PROVENANCE = ('HERMES_AGENT_TOOL_CALL', 'HERMES_AGENT_ORIGIN', 'HERMES_AGENT_HOME')


def test_desktop_relaunch_env_drops_tool_stamps(sentinel):
    """An agent's update/desktop command reopens Desktop with clean_launch_env: the relaunched
    Desktop's own HERMES_DESKTOP=1 backend is not agent-originated and must start."""
    from hermes_cli.top_level_env import clean_launch_env

    stamped = _clean_env(sentinel, extra={'HERMES_AGENT_TOOL_CALL': '1', 'HERMES_AGENT_ORIGIN': 'tool',
                                          'HERMES_AGENT_HOME': str(sentinel)})
    launch = clean_launch_env(stamped)
    assert not set(_PROVENANCE) & set(launch)
    launch['HERMES_DESKTOP'] = '1'  # what Electron sets on the backend it spawns
    result = _launch(sentinel, ['serve', '--port', '0'], env_override=launch)
    assert result.returncode == 87, (result.stdout, result.stderr)
    assert 'Refused proof backend' not in result.stderr


def test_desktop_top_level_launch_drops_tool_stamps(monkeypatch):
    from hermes_cli.top_level_env import scrub_for_top_level_launch

    for key in _PROVENANCE:
        monkeypatch.setenv(key, '1')
    scrub_for_top_level_launch(['desktop'])
    assert not set(_PROVENANCE) & set(os.environ)


def test_backend_launch_keeps_tool_stamps(monkeypatch):
    """serve/dashboard keep the stamps: startup recovery still needs to see the origin."""
    from hermes_cli.top_level_env import scrub_for_top_level_launch

    for key in _PROVENANCE:
        monkeypatch.setenv(key, '1')
    scrub_for_top_level_launch(['serve'])
    assert set(_PROVENANCE) <= set(os.environ)


def test_desktop_drop_list_is_the_provenance_stamps():
    """Pinned to the literal names. apps/desktop/electron/leaked-worker-env.ts AGENT_PROVENANCE_KEYS
    mirrors this list and its vitest pins the same literal, so drift on either side fails a test."""
    from hermes_cli.proof_home import PROVENANCE_KEYS

    assert PROVENANCE_KEYS == ('HERMES_AGENT_TOOL_CALL', 'HERMES_AGENT_ORIGIN', 'HERMES_AGENT_HOME')
