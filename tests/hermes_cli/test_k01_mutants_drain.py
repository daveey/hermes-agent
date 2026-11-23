"""Real child/lease proof for the offline helper; no native K01 probe is run."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import worker_drain_fence as fence
from tests.waiting import wait_until

SCRIPT = Path(__file__).resolve().parents[2] / 'apps/desktop/scripts/k01-mutants.py'


@pytest.fixture
def helper():
    spec = importlib.util.spec_from_file_location('k01_mutants', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def node(tmp_path, monkeypatch):
    ident = fence.Identity('i-0123456789abcdef0', fence.read_boot_id(), 'k01-test')
    fence.install_state(tmp_path, ident)
    monkeypatch.setattr(fence, 'load_activation', lambda *a: {'uid': os.getuid()})  # windows-footgun: ok — POSIX fixture
    monkeypatch.setattr(fence, '_activation_root', lambda act: tmp_path)
    monkeypatch.setattr(fence, 'current_identity', lambda *a, **k: ident)
    return tmp_path, ident


def phase(node, value):
    root, ident = node
    latch = fence.read_latch(root, ident)
    latch['phase'] = value
    fence._write_atomic(root / 'core-update' / fence.LATCH_FILE, latch)


@pytest.mark.parametrize('value', ['draining', 'sealed'])
def test_refuses_before_real_spawn(helper, node, value, tmp_path):
    marker = tmp_path / 'spawned'
    child = tmp_path / 'child.py'
    child.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    phase(node, value)
    with pytest.raises(fence.DrainRefused):
        helper.run_command([sys.executable, str(child)], timeout=5)
    assert not marker.exists()
    assert fence.busy_leases(node[0]) == 0


def test_real_child_holds_inherited_lease_until_exit(helper, node, tmp_path):
    ready, release = tmp_path / 'ready', tmp_path / 'release'
    child = tmp_path / 'child.py'
    child.write_text('import os, json, threading\nfrom pathlib import Path\n'
                     'fd = int(os.environ["HERMES_WORKER_DRAIN_LEASE_FD"])\n'
                     'os.fstat(fd)\n'
                     f'Path({str(ready)!r}).write_text(json.dumps(dict(pid=os.getpid(), fd=fd)))\n'
                     f'while not Path({str(release)!r}).exists(): threading.Event().wait(0.01)\n'
                     'print("finished")\n')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(helper.run_command, [sys.executable, str(child)],
                             timeout=15, stdout=subprocess.PIPE, check=True)
        try:
            wait_until(ready.exists, timeout=10)
            record = json.loads(ready.read_text())
            assert record['pid'] != os.getpid()  # windows-footgun: ok — POSIX fixture
            assert fence.busy_leases(node[0]) == 1
            phase(node, 'draining')
            assert fence.busy_leases(node[0]) == 1
            with pytest.raises(fence.DrainRefused):
                helper.run_command([sys.executable, str(child)], timeout=5)
        finally:
            release.touch()
        assert future.result(timeout=10).stdout == b'finished\n'
    assert fence.busy_leases(node[0]) == 0


def test_timeout_reaps_real_child_and_releases_lease(helper, node, tmp_path):
    child = tmp_path / 'child.py'
    child.write_text('import threading\nthreading.Event().wait(60)\n')
    with pytest.raises(subprocess.TimeoutExpired):
        helper.run_command([sys.executable, str(child)], timeout=0.2)
    assert fence.busy_leases(node[0]) == 0


def test_nonzero_check_and_capture(helper, node, tmp_path):
    child = tmp_path / 'child.py'
    child.write_text('import sys\nprint("output")\nsys.exit(7)\n')
    result = helper.run_command([sys.executable, str(child)], timeout=5, stdout=subprocess.PIPE)
    assert result.returncode == 7 and result.stdout == b'output\n'
    with pytest.raises(subprocess.CalledProcessError) as error:
        helper.run_command([sys.executable, str(child)], timeout=5,
                           stdout=subprocess.PIPE, check=True)
    assert error.value.returncode == 7 and error.value.output == b'output\n'
    assert fence.busy_leases(node[0]) == 0


def test_spawn_failure_releases_lease(helper, node, tmp_path):
    with pytest.raises(FileNotFoundError):
        helper.run_command([str(tmp_path / 'missing-executable')], timeout=5)
    assert fence.busy_leases(node[0]) == 0


def test_unactivated_child_still_runs(helper, monkeypatch, tmp_path):
    monkeypatch.setattr(fence, 'load_activation', lambda *a: None)
    child = tmp_path / 'child.py'
    child.write_text('import os\nprint(os.environ["HERMES_K01_COMPANION"])\n')
    result = helper.run_command([sys.executable, str(child)], timeout=5,
                                env={'HERMES_K01_COMPANION': 'fixture'}, stdout=subprocess.PIPE)
    assert result.returncode == 0 and result.stdout == b'fixture\n'
