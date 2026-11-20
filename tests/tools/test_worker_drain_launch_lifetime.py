"""Real child lifetime and terminal seam tests; no node or AWS writes."""
import os
import subprocess
import sys

import pytest

from hermes_cli import worker_drain_fence as f
from tests.tools import test_worker_drain_spawn as fixtures

node = fixtures.node
latch = fixtures.latch


def _launch(seam, argv, tmp_path):
    import shlex
    from hermes_cli.process_identity import spawn_owned
    from hermes_cli.local_runtime.processes import spawn_server
    from tools.environments.base_output import _popen_bash
    from tools.environments.local import LocalEnvironment
    from tools.browser_launcher import _spawn

    def local():
        env = LocalEnvironment.__new__(LocalEnvironment)
        env.env, env.cwd = {}, str(tmp_path)
        return env._run_bash(shlex.join(argv))

    return {
        'generic': lambda: f.popen_worker(subprocess.Popen, argv, env=None),
        'owned': lambda: spawn_owned(argv, purpose='drain-lifetime-fixture'),
        'bash': lambda: _popen_bash(argv),
        'browser': lambda: _spawn(argv, {}, str(tmp_path)),
        'router': lambda: spawn_server(argv)[0],
        'local': local,
    }[seam]()


@pytest.mark.parametrize('seam', ['generic', 'owned', 'bash', 'browser', 'router'])
def test_child_holds_lease_after_spawn_returns(node, tmp_path, seam):
    root, ident = node
    argv = [sys.executable, '-c', 'import time; time.sleep(30)']
    proc = _launch(seam, argv, tmp_path)
    try:
        assert f.busy_leases(root) == 1
        latch(root, ident)
        assert not f.seal_precheck(root, ident)
    finally:
        proc.kill()
        proc.wait(timeout=10)
    assert f.seal_precheck(root, ident)


def test_generic_preserves_explicit_pass_fds_and_cloexec(node):
    root, _ident = node
    read_fd, write_fd = os.pipe()
    try:
        proc = f.popen_worker(subprocess.Popen,
            [sys.executable, '-c', f'import os; os.write({write_fd}, b"ok")'],
            env=None, pass_fds=(write_fd,))
        assert os.read(read_fd, 2) == b'ok'
        assert proc.wait(timeout=10) == 0
        assert not os.get_inheritable(write_fd)
        assert f.busy_leases(root) == 0
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_spawn_failure_releases_lease(node):
    root, _ident = node
    with pytest.raises(FileNotFoundError):
        f.popen_worker(subprocess.Popen, ['/nonexistent-drain-fixture-command'], env=None)
    assert f.busy_leases(root) == 0


def test_pty_child_holds_lease_and_late_pty_refuses(node):
    from hermes_cli.pty_bridge import PtyBridge
    if not PtyBridge.is_available():
        pytest.skip('ptyprocess unavailable')
    root, ident = node
    bridge = PtyBridge.spawn([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        assert f.busy_leases(root) == 1
        latch(root, ident)
        assert not f.seal_precheck(root, ident)
        with pytest.raises(f.DrainRefused):
            PtyBridge.spawn([sys.executable, '-c', 'pass'])
    finally:
        bridge.close()


def test_reopened_unlocked_fd_cannot_mint_parent_authority(node, monkeypatch):
    root, ident = node
    parent = f.admit('fixture-parent')
    path = parent.path
    parent.close()
    fd = os.open(path, os.O_RDWR)
    try:
        monkeypatch.setenv(f.LEASE_FD_ENV, str(fd))
        latch(root, ident)
        with pytest.raises(f.DrainRefused):
            f.admit('forged-parent')
    finally:
        os.close(fd)


def test_nested_fenced_work_can_finish_after_latch(node):
    root, ident = node

    @f.fenced('already-admitted')
    def existing_work():
        latch(root, ident)
        proc = f.popen_worker(subprocess.Popen, [sys.executable, '-c', 'pass'])
        assert proc.wait(timeout=10) == 0
    existing_work()
    assert f.busy_leases(root) == 0


def test_seal_mutation_itself_checks_live_leases(node):
    root, ident = node
    parent = f.admit('fixture-parent')
    try:
        latch(root, ident)
        with pytest.raises(f.DrainRefused):
            latch(root, ident, op='seal')
        assert f.read_latch(root, ident)['phase'] == 'draining'
    finally:
        parent.close()
    assert latch(root, ident, op='seal')['phase'] == 'sealed'


@pytest.mark.parametrize('seam', ['generic', 'owned', 'bash', 'local', 'browser', 'router'])
def test_late_terminal_seams_cannot_execute(node, tmp_path, seam):
    root, ident = node
    latch(root, ident)
    output = tmp_path / 'should-not-exist'
    argv = [sys.executable, '-c', f'open({str(output)!r}, "w").write("lost")']
    with pytest.raises(f.DrainRefused):
        _launch(seam, argv, tmp_path)
    assert not output.exists()
