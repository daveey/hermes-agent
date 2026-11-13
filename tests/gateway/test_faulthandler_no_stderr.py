"""Regression: #71671 — gateway must survive faulthandler.enable() with sys.stderr=None."""

from __future__ import annotations

import sys

import pytest

from tests.waiting import wait_until


_SIGUSR2_CHILD = """
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[2])
from gateway.run_startup import GatewayStartupMixin
runner = object.__new__(GatewayStartupMixin)
runner.config = SimpleNamespace(log_dir=sys.argv[1])
runner._start_install_faulthandler()
Path(sys.argv[1], "ready").touch()
assert sys.stdin.readline() == "exit\\n"
"""


@pytest.mark.platforms("posix")
def test_sigusr2_stack_dump_leaves_the_gateway_running(tmp_path):
    """#110437 — SIGUSR2 must dump stacks to the log and return; chaining to the
    signal's default disposition (terminate) killed the process being inspected."""
    import os
    import signal
    import subprocess

    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    child = subprocess.Popen(
        [sys.executable, "-c", _SIGUSR2_CHILD, str(tmp_path), repo_root],
        stdin=subprocess.PIPE, text=True, encoding="utf-8",
    )

    def alive():
        assert child.poll() is None, f"gateway exited before handshake (rc={child.returncode})"
        return True

    dump_path = tmp_path / "gateway_faulthandler.log"

    def dumped():
        alive()
        return "--- end SIGUSR2 stack dump ---" in dump_path.read_text(encoding="utf-8")

    try:
        wait_until(lambda: alive() and (tmp_path / "ready").exists(),
                   timeout=30, what="gateway signal handler installed")
        child.send_signal(signal.SIGUSR2)
        # A scheduled child may not run within 0.5s. Observe the complete dump
        # before releasing it, rather than killing it with its signal pending.
        wait_until(dumped, timeout=30, what="complete SIGUSR2 stack dump")
        child.communicate("exit\n", timeout=30)
        assert child.returncode == 0, f"gateway died on SIGUSR2 (rc={child.returncode})"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=30)
        if child.stdin is not None:
            child.stdin.close()
    dump = dump_path.read_text(encoding="utf-8")
    assert "Thread 0x" in dump or "Current thread" in dump
