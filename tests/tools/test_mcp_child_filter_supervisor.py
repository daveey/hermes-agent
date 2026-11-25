"""MCP's spawn-delta filter never adopts the owner-death supervisor.

The supervisor is a direct child of this process, started lazily by the first owned spawn of any
family. When that happens between MCP's before/after child snapshots, the delta contains it; adopting
it would let MCP teardown group-kill the one process that reaps every other owned child.
"""

import subprocess
import sys

import pytest

from hermes_cli import process_identity
from tools import mcp_tool_lifecycle

pytestmark = pytest.mark.platforms("posix")


@pytest.mark.parametrize("empty_snapshots", [0, 3])
def test_an_owned_spawn_racing_mcp_discovery_does_not_hand_mcp_the_supervisor(monkeypatch, empty_snapshots):
    monkeypatch.setattr(process_identity, "_SUPERVISOR_IDLE_EXIT_S", 0.0)
    monkeypatch.setattr(process_identity, "_supervisor", None)
    monkeypatch.setattr(process_identity, "_supervised_pgids", set())
    monkeypatch.setattr(process_identity, "register_child", lambda *a, **k: True)
    before = mcp_tool_lifecycle._snapshot_child_pids()
    # Another family's first owned spawn lands inside MCP's before/after window: it starts the supervisor.
    worker = process_identity.spawn_owned([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                          purpose="test", stdin=subprocess.PIPE)
    mcp_server = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        import psutil

        original_cmdline = psutil.Process.cmdline
        remaining_empty = empty_snapshots

        def cmdline(proc):
            nonlocal remaining_empty
            if proc.pid == mcp_server.pid and remaining_empty:
                remaining_empty -= 1
                return []
            return original_cmdline(proc)

        monkeypatch.setattr(psutil.Process, "cmdline", cmdline)
        # Popen returning is not an inspectable-argv publication receipt. The
        # positive control must have exec'd before asking the fail-closed filter
        # to retain it; the supervisor's spawn-delta race remains unchanged.
        from tests.waiting import wait_until

        wait_until(lambda: psutil.Process(mcp_server.pid).cmdline() == mcp_server.args,
                   timeout=10, what="ordinary MCP child exec argv published")
        supervisor = process_identity._supervisor
        assert supervisor is not None
        delta = mcp_tool_lifecycle._snapshot_child_pids() - before
        assert supervisor.pid in delta, "precondition: the supervisor is in MCP's spawn delta"
        assert supervisor.pid not in mcp_tool_lifecycle._filter_mcp_children(delta)
        assert mcp_server.pid in mcp_tool_lifecycle._filter_mcp_children(delta)
    finally:
        for proc in (worker, mcp_server):
            proc.stdin.close()
            proc.wait(timeout=10)
        process_identity.release_process_group_once_empty(worker.pid, proc=worker, timeout=5)
        assert process_identity._supervisor is None, "releasing the last group stops the supervisor"


@pytest.mark.parametrize("empty_argv", [False, True])
def test_a_supervisor_whose_spawn_has_not_returned_yet_is_still_not_an_mcp_child(monkeypatch, empty_argv):
    """The returned supervisor is unpublished; exec can also transiently hide its argv."""
    import psutil

    monkeypatch.setattr(process_identity, "_supervisor", None)
    supervisor = process_identity._spawn_supervisor()  # running, but not yet published as _supervisor
    assert supervisor is not None
    original_cmdline = psutil.Process.cmdline
    inspected = []

    def cmdline(proc):
        if empty_argv and proc.pid == supervisor.pid:
            inspected.append(proc.pid)
            return []
        return original_cmdline(proc)

    monkeypatch.setattr(psutil.Process, "cmdline", cmdline)
    try:
        assert mcp_tool_lifecycle._filter_mcp_children({supervisor.pid}) == set()
        if empty_argv:
            assert inspected == [supervisor.pid]
    finally:
        supervisor.stdin.close()
        supervisor.wait(timeout=10)


def test_an_uninspectable_child_is_not_adopted_but_an_ordinary_mcp_child_is(monkeypatch):
    import psutil

    calls = []

    class Child:
        def __init__(self, pid):
            self.pid = pid

        def cmdline(self):
            calls.append(self.pid)
            return [] if self.pid == 1 else ["python", "fixture_mcp_server.py"]

    monkeypatch.setattr(psutil, "Process", Child)
    assert mcp_tool_lifecycle._filter_mcp_children({1, 2}) == {2}
    assert sorted(calls) == [1, 2]

