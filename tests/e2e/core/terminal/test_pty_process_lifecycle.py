"""Sandbox teardown stops the real owner, and never hides surviving infrastructure."""
from unittest.mock import MagicMock
import signal

import pytest

from tests.e2e.core.terminal import _pty
from hermes_cli.process_identity import _SUPERVISOR_SCRIPT

pytestmark = pytest.mark.platforms("linux")


def _inventory(monkeypatch, *, recycled=False):
    gateway, supervisor, viewer = 42001, 42002, 42000
    commands = {
        gateway: "python -m hermes_cli.main gateway run --quiet --idle-exit",
        supervisor: f"/bin/sh {_SUPERVISOR_SCRIPT} {gateway}",
        viewer: "python -m hermes_cli.main chat --cli",
    }
    monkeypatch.setattr(_pty, "cmdline", lambda pid: commands[pid])
    monkeypatch.setattr(_pty, "_start_time", lambda pid: 101 if recycled and pid == gateway else 100)
    monkeypatch.setattr(_pty, "session_members", lambda sid: [])
    term = object.__new__(_pty.PtyHermes)
    term.proc = MagicMock(poll=MagicMock(return_value=0))
    term.sid = viewer
    term.seen_members = {(gateway, 100), (supervisor, 100), (viewer, 100)}
    return term, gateway, supervisor


@pytest.mark.parametrize("recycled", [False, True])
def test_sandbox_shutdown_signals_only_its_live_gateway(monkeypatch, recycled):
    term, gateway, _supervisor = _inventory(monkeypatch, recycled=recycled)
    signals = []
    monkeypatch.setattr(_pty.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    term.stop_gateway()
    assert signals == ([] if recycled else [(gateway, signal.SIGTERM)])


def test_viewer_exit_keeps_only_the_identified_runtime(monkeypatch):
    import psutil

    term, gateway, supervisor = _inventory(monkeypatch)
    monkeypatch.setattr(psutil, "Process", lambda pid: MagicMock(ppid=lambda: gateway))
    monkeypatch.setattr(_pty.os, "getpgid", lambda pid: gateway)
    # Even a process that gateway shutdown would later reap must fail the viewer-exit check.
    leftovers = term.leftover_processes(timeout=0, runtime_alive=True)
    assert len(leftovers) == 1 and leftovers[0].startswith(f"{term.sid}:")
    assert _pty._gateway_runtime_members({gateway, supervisor}) == {gateway, supervisor}
    monkeypatch.setattr(psutil, "Process", lambda pid: MagicMock(ppid=lambda: term.sid))
    assert _pty._gateway_runtime_members({gateway, supervisor}) == {gateway}


def test_exit_inventory_never_exempts_gateway_or_supervisor(monkeypatch):
    term, gateway, supervisor = _inventory(monkeypatch)
    leftovers = term.leftover_processes(timeout=0)
    assert any(line.startswith(f"{gateway}:") for line in leftovers)
    assert any(line.startswith(f"{supervisor}:") for line in leftovers)


@pytest.mark.parametrize("surface", ["pty", "tmux"])
def test_startup_timeout_reports_screen_and_owned_processes(monkeypatch, surface):
    from tests.e2e.core.tui_pty import _helpers

    module = _pty if surface == "pty" else _helpers
    if surface == "pty":
        term = object.__new__(_pty.PtyHermes)
        term.sid = 42000
        wait = term.wait_ready
    else:
        term = object.__new__(_helpers.TmuxTui)
        term.pane_pid = 42000
        wait = term.wait_raw
    monkeypatch.setattr(term, "dump", lambda: "startup screen")
    monkeypatch.setattr(module, "session_members", lambda sid: [sid, 42001])
    monkeypatch.setattr(module, "cmdline", lambda pid: f"owned-child-{pid}")

    def timeout(*args, **kwargs):
        raise AssertionError("raw mode deadline")

    monkeypatch.setattr(module, "poll", timeout)
    with pytest.raises(AssertionError) as error:
        wait()
    message = str(error.value)
    assert "raw mode deadline" in message
    assert "startup screen" in message
    assert "owned-child-42000" in message
    assert "owned-child-42001" in message
