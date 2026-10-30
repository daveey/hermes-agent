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


def test_exit_inventory_never_exempts_gateway_or_supervisor(monkeypatch):
    term, gateway, supervisor = _inventory(monkeypatch)
    leftovers = term.leftover_processes(timeout=0)
    assert any(line.startswith(f"{gateway}:") for line in leftovers)
    assert any(line.startswith(f"{supervisor}:") for line in leftovers)
