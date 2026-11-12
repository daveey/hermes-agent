"""Process observation contracts for the real-fetch custody assertions."""

import subprocess
import sys
from pathlib import Path

import pytest

from tests.hermes_cli import test_update_custody as custody


@pytest.mark.parametrize(("state", "exited"), [
    ("X", True), ("Z", True), ("R", False), ("S", False),
    ("D", False), ("T", False), ("t", False), ("", False),
])
def test_fetch_exit_observation(monkeypatch, state, exited):
    # No host PID lookup: retain a proc entry even for the rare Linux dead state.
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "read_text", lambda self, **kw: f"123 (git fetch) {state}")
    assert custody._fetch_exited(123) is exited


@pytest.mark.parametrize("error", [FileNotFoundError, ProcessLookupError, PermissionError, OSError])
def test_fetch_exit_observation_read_errors(monkeypatch, error):
    # The entry existed at a prior observation, but stat may disappear before read.
    monkeypatch.setattr(Path, "exists", lambda self: True)

    def read(self, **kw):
        raise error("proc observation")

    monkeypatch.setattr(Path, "read_text", read)
    if error in (FileNotFoundError, ProcessLookupError):
        assert custody._fetch_exited(123)
    else:
        with pytest.raises(error, match="proc observation"):
            custody._fetch_exited(123)


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("after_open", [False, True], ids=["before-open", "before-read"])
def test_fetch_exit_observation_during_real_reaping(monkeypatch, after_open):
    # A pipe keeps our child alive without a timer. Reap at the actual procfs
    # boundary: ENOENT before open, ESRCH when reading an already-open inode.
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE,
    )
    target = Path(f"/proc/{child.pid}/stat")
    original_open = Path.open
    observed = []

    def open_then_reap(path, *args, **kwargs):
        if path != target:
            return original_open(path, *args, **kwargs)
        stream = original_open(path, *args, **kwargs) if after_open else None
        try:
            child.kill()
            child.wait(timeout=30)
            observed.append(after_open)
        except BaseException:
            if stream is not None:
                stream.close()
            raise
        return stream if stream is not None else original_open(path, *args, **kwargs)

    try:
        assert not custody._fetch_exited(child.pid), "live child misclassified as exited"
        monkeypatch.setattr(Path, "open", open_then_reap)
        assert custody._fetch_exited(child.pid)
        assert observed == [after_open]
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=30)
        assert child.stdin is not None
        child.stdin.close()
