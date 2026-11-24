"""A hard-kill target that exits during the stop handshake must not burn the stop budget.

``_process_tree_snapshot`` read ``status()`` once, suspended a running target, then waited for
``STOPPED``. A target that exits between those reads is a zombie until its parent reaps it, never
``STOPPED``: the wait spun the whole ``_TREE_STOP_DEADLINE_S`` (5 s). That is what made
``test_faults_are_typed_and_reaped[os._exit(7)]`` take 5.03 s on CI (PR #4233, shard 3/12).
"""
import subprocess
import sys
import time

import pytest


@pytest.mark.platforms("linux")
def test_target_that_turns_zombie_after_the_first_status_read_is_not_waited_out(monkeypatch):
    import psutil
    from agent import deadline

    proc = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target = psutil.Process(proc.pid)
        stamp = deadline.process_create_time(proc.pid)
        wait = time.monotonic() + 10
        while target.status() != psutil.STATUS_ZOMBIE:  # exited, not reaped: the CI state
            assert time.monotonic() < wait, "child never became a zombie"
            time.sleep(0.01)

        real_status = psutil.Process.status
        lied = []

        def status(self):
            # The first read races the exit: the target was still running when sampled.
            if self.pid == proc.pid and not lied:
                lied.append(True)
                return psutil.STATUS_RUNNING
            return real_status(self)

        monkeypatch.setattr(psutil.Process, "status", status)
        started = time.monotonic()
        deadline.kill_process_tree(proc.pid, create_time=stamp)
        elapsed = time.monotonic() - started
        assert lied, "the race window was not exercised"
        assert elapsed < 1.0, f"stop handshake waited {elapsed:.2f}s on a zombie"
    finally:
        proc.wait(timeout=10)
