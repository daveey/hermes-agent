"""Probe-only processes participate in the same pre-rmtree custody barrier."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_constants import mark_named_profile_deleted
from hermes_profile_resources import wait_profile_resources
from tests.hermes_state import test_profile_retirement_custody as helpers

profile_home = helpers.profile_home


@pytest.mark.parametrize('kind', ['header', 'pread'])
def test_remote_probe_only_process_drains_before_unlink(profile_home, kind):
    target = profile_home / 'probe.sqlite3-shm'
    target.write_bytes(b'probe')
    code = '''
import os, sys
from pathlib import Path
from hermes_state_dbfile import _pread_db_range, header_probes_under
from hermes_cli.sqlite_safe_read import pread_lock_safe, lock_safe_fds
home, kind = Path(sys.argv[1]), sys.argv[2]
path = home / 'probe.sqlite3-shm'
if kind == 'header':
    assert _pread_db_range(path, 0, 5) == b'probe'
    fds = [entry[0] for entry in header_probes_under(home).values()]
else:
    assert pread_lock_safe(path, 0, 5) == b'probe'
    fds = list(lock_safe_fds())
assert fds
print('ready', flush=True)
sys.stdin.readline()
for fd in fds:
    try:
        os.fstat(fd)
    except OSError:
        continue
    raise AssertionError('raw probe descriptor still open')
print('closed', flush=True)
'''
    child = subprocess.Popen([sys.executable, '-c', code, str(profile_home), kind],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                             env=dict(os.environ), cwd=Path(__file__).resolve().parents[2])
    try:
        assert child.stdout.readline().strip() == 'ready'
        mark_named_profile_deleted(profile_home)
        assert wait_profile_resources(profile_home, timeout=10)
        assert target.exists()  # acknowledgement preceded unlink, not just eventual cleanup
        out, _ = child.communicate('check\n', timeout=10)
        assert out.strip() == 'closed'
        assert child.returncode == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(10)


def test_monitor_start_failure_leaves_no_false_custody(profile_home):
    code = '''
import sys, threading
from pathlib import Path
import hermes_profile_resources as resources
from hermes_constants import mark_named_profile_deleted
home = Path(sys.argv[1])
original = threading.Thread.start
def fail(self):
    raise RuntimeError('controlled thread start failure')
threading.Thread.start = fail
try:
    resources.lease_for_path(home / 'state.db')
except RuntimeError as exc:
    assert str(exc) == 'controlled thread start failure'
else:
    raise AssertionError('start should fail')
finally:
    threading.Thread.start = original
assert resources._monitor is None
assert not resources._leases
mark_named_profile_deleted(home)
assert resources.wait_profile_resources(home, timeout=0)
'''
    result = subprocess.run([sys.executable, '-c', code, str(profile_home)], capture_output=True,
                            text=True, timeout=15, cwd=Path(__file__).resolve().parents[2], check=False)
    assert result.returncode == 0, (result.stdout, result.stderr)
