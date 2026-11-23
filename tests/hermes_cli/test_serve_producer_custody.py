"""Real inherited lock custody survives broker close until its child exits."""
import os
import select
import subprocess
import sys

import pytest

from tools.environments.local import build_subprocess_env


@pytest.mark.platforms('posix')
def test_child_keeps_fence_after_broker_closes_its_descriptor(tmp_path):
    from hermes_cli import serve_producer_custody as custody

    runtime = tmp_path / 'producer-runtime'
    owner = custody.FamilyCustody.acquire(runtime, 'panel')
    identity = (runtime / 'panel.lock').stat().st_ino
    script = tmp_path / 'holder.py'
    script.write_text('import os, sys\nos.write(int(sys.argv[1]), b"R")\nsys.stdin.buffer.read(1)\n')
    ready_read, ready_write = os.pipe()
    child = None
    try:
        child = subprocess.Popen(
            [sys.executable, '-I', str(script), str(ready_write)],
            pass_fds=(owner.fd, ready_write), stdin=subprocess.PIPE,
            env=build_subprocess_env(),
        )
        os.close(ready_write)
        ready_write = -1
        assert select.select([ready_read], [], [], 10)[0], 'holder did not become ready'
        assert os.read(ready_read, 1) == b'R'
        owner.close()
        with pytest.raises(custody.CustodyConflict):
            custody.FamilyCustody.acquire(runtime, 'panel')
        child.communicate(b'X', timeout=10)
        assert child.returncode == 0
        successor = custody.FamilyCustody.acquire(runtime, 'panel')
        successor.close()
        assert (runtime / 'panel.lock').stat().st_ino == identity
    finally:
        owner.close()
        if child is not None and child.poll() is None:
            child.communicate(b'X', timeout=10)
        os.close(ready_read)
        if ready_write >= 0:
            os.close(ready_write)


@pytest.mark.platforms('posix')
def test_rejects_unsafe_runtime_and_symlink_lock_without_touching_target(tmp_path):
    from hermes_cli import serve_producer_custody as custody

    runtime = tmp_path / 'producer-runtime'
    runtime.mkdir(mode=0o755)
    with pytest.raises(custody.CustodyUnsafe, match='private directory'):
        custody.FamilyCustody.acquire(runtime, 'panel')
    runtime.chmod(0o700)
    target = tmp_path / 'untouched'
    target.write_bytes(b'original')
    (runtime / 'panel.lock').symlink_to(target)
    with pytest.raises(custody.CustodyUnsafe, match='lock file'):
        custody.FamilyCustody.acquire(runtime, 'panel')
    assert target.read_bytes() == b'original'
