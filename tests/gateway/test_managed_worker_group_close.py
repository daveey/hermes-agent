"""Owner-initiated managed-worker teardown reaps the worker's process group (N55).

The worker leads its own process group (WORKER_POPEN start_new_session). When the owner ends it
(Stop not acknowledged, lost/cancelled worker, gateway shutdown), close() signalled the worker PID
only; the worker died before it could see stdin EOF, so _die_with_owner's group kill never ran and
its tool children were orphaned outside every group the gateway's supervisors reach.
Real processes and the real close().
"""
import os
import subprocess
import sys
from contextlib import suppress

import psutil
import pytest

from gateway.session_managed_worker import WORKER_POPEN, ManagedWorker
from tests.waiting import wait_until

_WORKER = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(120)\n")


def _gone(pid):
    # Reaping walks a pid Z -> X (dead) -> unhashed; counting only Z read a dead pid as alive again.
    try:
        return psutil.Process(pid).status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
    except psutil.NoSuchProcess:
        return True


@pytest.mark.platforms("linux", "macos")
@pytest.mark.live_system_guard_bypass  # cleanup may kill our own grandchild after init adopted it (pre-fix)
def test_close_reaps_the_workers_tool_children():
    process = subprocess.Popen([sys.executable, '-c', _WORKER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, **WORKER_POPEN)
    grandchild = int(process.stdout.readline())
    try:
        assert os.getpgid(grandchild) == process.pid
        ManagedWorker(process).close()
        assert process.poll() is not None
        wait_until(lambda: _gone(grandchild), what="the worker's tool child to die with the owner's close()")
    finally:
        if not _gone(grandchild):  # an orphan (pre-fix) is reparented out of the test's subtree
            with suppress(ProcessLookupError):
                os.kill(grandchild, 9)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
