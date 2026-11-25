"""A natural update must refresh the controller consumer, not just backend code."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import always_on_deployment as deployment
from hermes_cli import always_on_rollover as ro


def test_natural_roll_replaces_legacy_entrypoint_before_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    entry = tmp_path / '.local/share/hermes-always-on/always_on_dashboard.py'
    entry.parent.mkdir(parents=True)
    entry.write_text('def _interim_main():\n    return 0\n')
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        # A natural controller restart must no longer execute the copied implementation.
        assert '_interim_main' not in entry.read_text()
        assert 'hermes_cli' in entry.read_text()
        return subprocess.CompletedProcess(argv, 0)

    assert ro.request_roll(run=run)
    assert calls == [['systemctl', '--user', 'restart', '--no-block', ro.ROLLER_UNIT]]
    # Same command shape as the deployed unit, without starting a controller or touching services.
    result = subprocess.run([sys.executable, '-S', str(entry), 'run', '--help'],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert '--port' in result.stdout
    assert ro.request_roll(run=run)  # idempotent after migration


def test_no_standalone_install_does_not_create_one(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    calls = []
    assert ro.request_roll(run=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0))
    assert len(calls) == 1
    assert not (tmp_path / '.local/share/hermes-always-on').exists()


@pytest.mark.parametrize('symlink', [False, True])
def test_unknown_or_symlink_entrypoint_is_preserved_without_restart(tmp_path, monkeypatch, symlink):
    monkeypatch.setenv('HOME', str(tmp_path))
    entry = tmp_path / '.local/share/hermes-always-on/always_on_dashboard.py'
    entry.parent.mkdir(parents=True)
    original = 'raise RuntimeError("custom controller")\n'
    if symlink:
        target = tmp_path / 'custom.py'
        target.write_text(original)
        entry.symlink_to(target)
    else:
        entry.write_text(original)
    calls = []
    assert not ro.request_roll(run=lambda argv, **kw: calls.append(argv))
    assert not calls
    assert entry.read_text() == original


def test_dangling_symlink_entrypoint_is_preserved_without_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    entry = tmp_path / '.local/share/hermes-always-on/always_on_dashboard.py'
    entry.parent.mkdir(parents=True)
    target = tmp_path / 'missing-controller.py'
    entry.symlink_to(target)
    calls = []
    assert not ro.request_roll(run=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0))
    assert not calls
    assert entry.is_symlink()
    assert entry.readlink() == target
    assert not target.exists()


def test_loader_uses_current_checkout_not_adjacent_stale_roller(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    core = tmp_path / 'installed core'
    package = core / 'hermes_cli'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (package / 'always_on_rollover.py').write_text('SUPERSEDED_GRACE_S = 30\n')
    (package / 'always_on_dashboard.py').write_text('''from .always_on_rollover import SUPERSEDED_GRACE_S

def build_always_on_dashboard_parser(subparsers):
    parser = subparsers.add_parser('always-on-dashboard')
    parser.add_argument('action')
    parser.add_argument('--port')
    parser.add_argument('--hermes')
    parser.set_defaults(func=lambda args: print(args.action, args.port, args.hermes, SUPERSEDED_GRACE_S) or 0)
''')
    monkeypatch.setattr(deployment, 'project_root_str', lambda: str(core))
    entry = tmp_path / '.local/share/hermes-always-on/always_on_dashboard.py'
    entry.parent.mkdir(parents=True)
    entry.write_text('def _interim_main():\n    return 0\n')
    stale = entry.with_name('always_on_rollover.py')
    stale.write_text('raise RuntimeError("stale roller imported")\n')
    deployment.refresh_standalone_controller()
    inode = entry.stat().st_ino
    deployment.refresh_standalone_controller()
    assert entry.stat().st_ino == inode  # no-op refresh, not an unnecessary replace
    result = subprocess.run([sys.executable, '-S', str(entry), 'backend', '--port', '9150', '--hermes', '/a b/hermes'],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'backend 9150 /a b/hermes 30'
    assert stale.read_text() == 'raise RuntimeError("stale roller imported")\n'
    assert entry.stat().st_mode & 0o777 == 0o600


def test_failed_entrypoint_publication_does_not_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    entry = tmp_path / '.local/share/hermes-always-on/always_on_dashboard.py'
    entry.parent.mkdir(parents=True)
    original = 'def _interim_main():\n    return 0\n'
    entry.write_text(original)

    def fail(*args):
        raise OSError('publication failed')

    monkeypatch.setattr(deployment.aod, '_write_private', fail)
    calls = []
    assert not ro.request_roll(run=lambda argv, **kw: calls.append(argv))
    assert not calls
    assert entry.read_text() == original
