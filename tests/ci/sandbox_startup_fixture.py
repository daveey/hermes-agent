"""Test-only checkpoint protocol and an out-of-process descendant ownership fence."""
from __future__ import annotations

import ctypes
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time


CHECKPOINTS = ("monitor", "killer")


def checkpoint(control, name, before_stop=None):
    """Return False when disarmed; acknowledge a stop by releasing its lock."""
    control = Path(control)
    with (control / f"{name}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (control / "disarmed").exists():
            return False
        birth = Path(f"/proc/{os.getpid()}/stat").read_text().rpartition(")")[2].split()[19]
        _publish(control / f"{name}.identity", [os.getpid(), birth])
        (control / name).write_text(str(os.getpid()))
        if before_stop is not None:
            before_stop()
        os.kill(os.getpid(), signal.SIGSTOP)
    return True


def _resume(control, name):
    try:
        pid, birth = json.loads((control / f"{name}.identity").read_text())
        fd = os.pidfd_open(pid)
    except (FileNotFoundError, ProcessLookupError):
        return
    try:
        try:
            current = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19]
            if current == birth:
                signal.pidfd_send_signal(fd, signal.SIGCONT)
        except (FileNotFoundError, ProcessLookupError):
            pass
    finally:
        os.close(fd)


def disarm(control):
    """Prevent future stops and drain stops already admitted, even before discovery."""
    from tests.waiting import wait_until

    (control / "disarmed").touch()
    for name in CHECKPOINTS:
        with (control / f"{name}.lock").open("w") as lock:
            def drained():
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    _resume(control, name)
                    return False
                return True
            # A single SIGCONT can arrive before SIGSTOP. Only lock acquisition
            # acknowledges that the holder has passed the stop (or died).
            wait_until(drained, timeout=30)


def _publish(path, value):
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(value))
    pending.replace(path)


def _reap_owned():
    """Only this disposable process is a subreaper; ECHILD is the receipt."""
    deadline = time.monotonic() + 30
    children = Path(f"/proc/self/task/{os.getpid()}/children")
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid:
            continue
        if time.monotonic() >= deadline:
            raise TimeoutError("test fence could not reap its descendants")
        # Single-threaded owner: none of these unreaped children can have its PID
        # reused between enumeration and signalling. Killing parents adopts the
        # next generation, including setsid descendants and late namespace init.
        for child in children.read_text().split():
            try:
                os.kill(int(child), signal.SIGKILL)
            except ProcessLookupError:
                pass
        select.select([], [], [], 0.01)


def _serve(control, argv):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot establish test ownership fence")
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
    _publish(control / "wrapper", proc.pid)
    pending = b""
    try:
        while True:
            status = proc.poll()
            if status is not None:
                _publish(control / "status", status)
            if not select.select([0], [], [], 0.01)[0]:
                continue
            data = os.read(0, 4096)
            if not data:
                return
            pending += data
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                proc.send_signal(int(line))
    finally:
        _reap_owned()
        _publish(control / "reaped", True)


class FencedProcess:
    """Popen-like controller; the fence does nothing to descendants until close."""

    def __init__(self, control, argv, **kwargs):
        self.control = control
        self.fence = subprocess.Popen(
            [sys.executable, "-I", __file__, str(control), *argv],
            stdin=subprocess.PIPE, start_new_session=True, **kwargs,
        )
        assert self.fence.stdin is not None
        self.commands = self.fence.stdin

    def send_signal(self, signum):
        self.commands.write(f"{int(signum)}\n".encode())
        self.commands.flush()

    def wait(self, timeout):
        from tests.waiting import wait_until

        wait_until(lambda: (self.control / "status").exists(), timeout=timeout)
        return json.loads((self.control / "status").read_text())

    def close(self):
        try:
            disarm(self.control)
        finally:
            # EOF also works if pytest dies. The fence kills stopped children;
            # it does not need checkpoint publication or an init pidfd to own them.
            self.commands.close()
            status = self.fence.wait(timeout=40)
            assert status == 0, f"ownership fence exited {status}"
            assert json.loads((self.control / "reaped").read_text()) is True


if __name__ == "__main__":
    _serve(Path(sys.argv[1]), sys.argv[2:])
