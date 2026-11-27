"""Aggregate setup must use the same verified bus as restart-safe worker launch."""
import json
import os
from pathlib import Path
import socket
import sys

import pytest

from hermes_cli import kanban_aggregate_scope as scope
from tools import process_registry as pr


@pytest.fixture
def bus_setup(tmp_path, monkeypatch):
    if sys.platform != 'linux':
        pytest.skip('Linux user bus')
    # AF_UNIX paths are bounded; pytest's nested tmp_path can exceed sun_path.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='kb-bus-') as directory:
        runtime = Path(directory)
        runtime.chmod(0o700)
        bus = socket.socket(socket.AF_UNIX)
        bus.bind(str(runtime / 'bus'))
        monkeypatch.setattr(pr, '_default_user_runtime_dir', lambda: runtime)
        monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
        monkeypatch.delenv('DBUS_SESSION_BUS_ADDRESS', raising=False)
        scope.configure_pool.cache_clear()
        try:
            yield runtime
        finally:
            scope.configure_pool.cache_clear()
            bus.close()


def fake_systemctl(tmp_path, monkeypatch, runtime):
    """Execute a real child, checking its environment and exact caps/arguments."""
    executable = tmp_path / 'systemctl'
    report = tmp_path / 'report.json'
    executable.write_text(
        f'#!{sys.executable}\n'
        'import json, os, sys\n'
        f'expected = {str(runtime)!r}\n'
        f'out = {str(report)!r}\n'
        'env = {k: os.environ.get(k) for k in '
        '("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")}\n'
        'with open(out, "w") as f: json.dump([sys.argv[1:], env], f)\n'
        'sys.exit(0 if env == {"XDG_RUNTIME_DIR": expected, '
        '"DBUS_SESSION_BUS_ADDRESS": "unix:path=" + expected + "/bus"} else 1)\n')
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ.get('PATH', ''))
    return report


@pytest.mark.parametrize('present', [False, True])
def test_pool_child_gets_verified_bus_and_unchanged_caps(bus_setup, tmp_path, monkeypatch, present):
    runtime = bus_setup
    if present:
        monkeypatch.setenv('XDG_RUNTIME_DIR', str(runtime))
        monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', f'unix:path={runtime}/bus')
    before = dict(os.environ)
    report = fake_systemctl(tmp_path, monkeypatch, runtime)
    # PATH is the only mutation after before; discovery must not export bus vars.
    before['PATH'] = os.environ['PATH']
    scope.configure_pool()
    args, env = json.loads(report.read_text())
    assert args == ['--user', 'set-property', '--runtime', 'hermes-kanban.slice',
                    'MemoryHigh=70%', 'MemoryMax=80%']
    assert env['XDG_RUNTIME_DIR'] == str(runtime)
    assert dict(os.environ) == before


@pytest.mark.parametrize('invalid', ['missing', 'file', 'symlink', 'writable', 'foreign'])
def test_unverified_bus_never_returns_unbounded_command(bus_setup, tmp_path, monkeypatch, invalid):
    runtime = bus_setup
    (runtime / 'bus').unlink()
    actions = {
        'missing': lambda: None,
        'file': lambda: (runtime / 'bus').touch(),
        'symlink': lambda: (runtime / 'bus').symlink_to(tmp_path / 'absent'),
        'writable': lambda: runtime.chmod(0o777),
        'foreign': lambda: monkeypatch.setattr(pr.os, 'getuid', lambda: runtime.stat().st_uid + 1),
    }
    actions[invalid]()
    report = fake_systemctl(tmp_path, monkeypatch, runtime)
    monkeypatch.setattr(scope.time, 'sleep', lambda _: None)
    with pytest.raises(pr.RestartSafeScopeUnavailable, match='aggregate'):
        scope.bounded_scope(['systemd-run', '--user', '--scope', '--', 'worker'])
    _, env = json.loads(report.read_text())
    assert env == {'XDG_RUNTIME_DIR': None, 'DBUS_SESSION_BUS_ADDRESS': None}
    assert scope.configure_pool.cache_info().currsize == 0
