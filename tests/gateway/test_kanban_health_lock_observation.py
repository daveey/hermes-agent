"""Health ownership must not depend on a changing host-wide lock-table snapshot."""
import fcntl
import os
import subprocess
import sys
from pathlib import Path

from tools.environments.local import build_subprocess_env

import pytest

from gateway import kanban_dispatcher_health as health

pytestmark = pytest.mark.platforms("linux")


@pytest.mark.parametrize("mode", ["exclusive", "shared", "posix", "unlocked"])
def test_health_observes_exact_process_flock(tmp_path, monkeypatch, mode):
    path = tmp_path / "dispatcher.lock"
    with path.open("w") as handle:
        if mode == "posix":
            fcntl.lockf(handle, fcntl.LOCK_EX)
        elif mode != "unlocked":
            fcntl.flock(handle, fcntl.LOCK_EX if mode == "exclusive" else fcntl.LOCK_SH)
        # A /proc/locks multi-read can miss this entry as unrelated locks churn.
        # Keep the actual kernel lock and per-process observations intact.
        read_text = Path.read_text

        def snapshot(file, *args, **kwargs):
            if file == Path("/proc/locks"):
                return ""
            return read_text(file, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", snapshot)
        assert health._owns_lock(path, os.getpid()) is (mode == "exclusive")
        assert not health._owns_lock(path, os.getppid())
    assert not health._owns_lock(path, os.getpid())


def test_inherited_descriptor_does_not_make_child_the_owner(tmp_path):
    path = tmp_path / "dispatcher.lock"
    with path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        code = (
            "import os, sys; from pathlib import Path; "
            "from gateway.kanban_dispatcher_health import _owns_lock; "
            "path = Path(sys.argv[1]); "
            "assert not _owns_lock(path, os.getpid()); "
            "assert _owns_lock(path, os.getppid())"
        )
        subprocess.run(
            [sys.executable, "-c", code, str(path)], pass_fds=(handle.fileno(),),
            env=build_subprocess_env(), check=True, timeout=30,
            capture_output=True, text=True, encoding="utf-8",
        )


@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError])
def test_descriptor_observation_failure_is_unavailable(tmp_path, monkeypatch, failure):
    path = tmp_path / "dispatcher.lock"
    with path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        read_text = Path.read_text
        descriptor = Path(f"/proc/{os.getpid()}/fdinfo/{handle.fileno()}")

        def unavailable(file, *args, **kwargs):
            if file == descriptor:
                raise failure("descriptor unavailable")
            return read_text(file, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", unavailable)
        assert not health._owns_lock(path, os.getpid())


def test_replaced_inode_is_not_the_owned_lock(tmp_path):
    path = tmp_path / "dispatcher.lock"
    with path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        assert health._owns_lock(path, os.getpid())
        path.unlink()
        path.touch()
        assert not health._owns_lock(path, os.getpid())
