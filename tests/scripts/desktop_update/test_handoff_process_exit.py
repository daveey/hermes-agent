"""The handoff observer must tolerate reaping during a procfs read."""

from pathlib import Path
import subprocess

import pytest

from tests.scripts.desktop_update.test_desktop_update_posix_handoff_protocol import _process_exited

pytestmark = pytest.mark.platforms("linux")


@pytest.mark.parametrize("boundary", ["before-open", "after-open"])
def test_reaped_process_is_exited_even_during_stat_read(monkeypatch, boundary):
    child = subprocess.Popen(["sleep", "120"])
    stat = Path(f"/proc/{child.pid}/stat")
    original_open = Path.open
    reached = False

    def reap_then_read(path, *args, **kwargs):
        nonlocal reached
        if path != stat:
            return original_open(path, *args, **kwargs)
        reached = True
        stream = original_open(path, *args, **kwargs) if boundary == "after-open" else None
        child.kill()
        child.wait(timeout=30)
        # Before open yields ENOENT; an already-open proc inode yields ESRCH on read.
        return stream if stream is not None else original_open(path, *args, **kwargs)

    try:
        assert not _process_exited(child.pid)
        monkeypatch.setattr(Path, "open", reap_then_read)
        assert _process_exited(child.pid)
        assert reached
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=30)


@pytest.mark.parametrize("state, exited", [("R", False), ("S", False), ("T", False), ("Z", True), ("X", True)])
def test_process_exit_states(monkeypatch, state, exited):
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "read_text", lambda self, **kw: f"123 (name with ) spaces) {state} 1 2 3")
    assert _process_exited(123) is exited


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_process_exit_does_not_hide_unexpected_errors(monkeypatch, error):
    def denied(self, **kwargs):
        raise error("procfs inaccessible")

    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(error, match="procfs inaccessible"):
        _process_exited(123)
