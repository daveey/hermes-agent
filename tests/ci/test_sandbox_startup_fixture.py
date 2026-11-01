"""Real-process failure injection for the startup proof's own teardown protocol."""
from __future__ import annotations

import os
from pathlib import Path
import select
import signal
import sys
import textwrap

import pytest

from tests.ci import sandbox_startup_fixture as fixture
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms("linux")


def _command(control, body):
    return [sys.executable, "-I", "-c", textwrap.dedent(f"""\
        import os, signal, sys
        from pathlib import Path
        sys.path.insert(0, {str(Path(fixture.__file__).parent)!r})
        from sandbox_startup_fixture import checkpoint
        control = Path({str(control)!r})
    """) + textwrap.dedent(body)]


@pytest.mark.parametrize("name", fixture.CHECKPOINTS)
@pytest.mark.parametrize("phase", ["before_entry", "publication_gap", "stopped"])
def test_disarm_acknowledges_checkpoint_without_discovered_pid(tmp_path, monkeypatch, name, phase):
    # No stopper PID is supplied to disarm. In publication_gap the first CONT
    # deliberately arrives BEFORE SIGSTOP; it must not be mistaken for an ack.
    command = _command(tmp_path, f"""\
        import select
        phase = {phase!r}
        def before_stop():
            if phase != "publication_gap":
                return
            received = []
            signal.signal(signal.SIGCONT, lambda *_: received.append(True))
            (control / "ready").touch()
            while not received:
                select.select([], [], [], 0.01)
        if phase == "before_entry":
            (control / "ready").touch()
            while not (control / "disarmed").exists():
                select.select([], [], [], 0.01)
        admitted = checkpoint(control, {name!r}, before_stop)
        (control / "finished").write_text(str(admitted))
    """)
    proc = fixture.FencedProcess(tmp_path, command)
    real_kill = signal.pidfd_send_signal
    continues = []

    def counted_kill(pid, signum):
        if signum == signal.SIGCONT:
            continues.append(pid)
        return real_kill(pid, signum)

    try:
        if phase == "stopped":
            def stopped():
                try:
                    pid = int((tmp_path / name).read_text())
                    return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0] == "T"
                except (FileNotFoundError, ValueError):
                    return False
            wait_until(stopped, timeout=30)
        else:
            wait_until(lambda: (tmp_path / "ready").exists(), timeout=30)
        monkeypatch.setattr(fixture.signal, "pidfd_send_signal", counted_kill)
        fixture.disarm(tmp_path)
        # Assert acknowledgment BEFORE fence cleanup, so SIGKILL cannot hide a
        # broken disarm protocol or make a sentinel-only mutation pass.
        assert proc.wait(timeout=30) == 0
        assert (tmp_path / "finished").read_text() == str(phase != "before_entry")
        if phase == "publication_gap":
            assert len(continues) >= 2
    finally:
        proc.close()
    assert not Path(f"/proc/{proc.fence.pid}").exists()


def test_fence_reaps_unknown_stopped_session_and_late_child(tmp_path):
    command = _command(tmp_path, """\
        # This tree deliberately ignores the checkpoint protocol. Ownership must
        # survive setsid, stripped environment, and a new child after wrapper exit.
        child = os.fork()
        if child:
            os._exit(0)
        os.setsid()
        os.environ.clear()
        grandchild = os.fork()
        if not grandchild:
            (control / "grandchild").write_text(str(os.getpid()))
            os.kill(os.getpid(), signal.SIGSTOP)
            os._exit(0)
        (control / "detached").write_text(str(os.getpid()))
        os.kill(os.getpid(), signal.SIGSTOP)
        os._exit(0)
    """)
    proc = fixture.FencedProcess(tmp_path, command)
    fds = []
    try:
        assert proc.wait(timeout=30) == 0
        for name in ("detached", "grandchild"):
            wait_until(lambda: (tmp_path / name).exists() and (tmp_path / name).read_text(), timeout=30)
            pid = int((tmp_path / name).read_text())
            fds.append(os.pidfd_open(pid))
        assert not select.select(fds, [], [], 0)[0], "fence must be idle until cleanup"
    finally:
        try:
            proc.close()
            assert len(select.select(fds, [], [], 0)[0]) == len(fds)
        finally:
            for fd in fds:
                os.close(fd)
    for name in ("detached", "grandchild", "wrapper"):
        assert not Path(f"/proc/{int((tmp_path / name).read_text())}").exists()
