"""A PTY session keeps the output its child wrote before the reader thread first ran [daveey].

``PtyProcess.isalive()`` turns False once the child is reaped, so a reader starved of CPU from spawn to
the child's exit used to leave its loop without reading anything and publish an exited session with an
empty output (CI flake of ``TestStdinHelpers.test_close_stdin_allows_eof_driven_process_to_finish``:
``assert 'hello' in ''``). The reader now drains what the master already holds before it records the exit.
"""

from __future__ import annotations

import os
import shlex
import sys

import pytest
from ptyprocess import PtyProcess

from tests.waiting import wait_until
from tools.process_registry import ProcessRegistry


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr("hermes_cli.process_identity._update_supervised_pgids", lambda verb, pgids: None)
    return ProcessRegistry()


@pytest.mark.platforms("linux")
def test_reader_that_first_runs_after_child_exit_keeps_its_output(registry, tmp_path, monkeypatch):
    real_isalive = PtyProcess.isalive
    gated = set()

    def isalive_after_exit(self):
        # Hold the reader's first liveness check until the child has exited (WNOWAIT leaves it
        # unreaped for ptyprocess): the schedule of a reader starved from spawn to exit.
        if self.pid not in gated:
            gated.add(self.pid)
            os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOWAIT)
        return real_isalive(self)

    monkeypatch.setattr(PtyProcess, "isalive", isalive_after_exit)
    command = f"{shlex.quote(sys.executable)} -c \"print('hello')\""
    session = registry.spawn_local(command, cwd=str(tmp_path), use_pty=True)

    poll = wait_until(lambda: (p := registry.poll(session.id))["status"] == "exited" and p,
                      timeout=30, what="PTY session exited")
    assert poll["exit_code"] == 0
    assert "hello" in poll["output_preview"]
