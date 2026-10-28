"""Worker exit classification must not depend on POSIX-only ``os.WIF*`` helpers.

``_classify_worker_exit`` feeds the dead-worker reclaim: a ``rate_limited``
verdict requeues the card without counting a failure, ``unknown`` counts as a
crash. Windows has neither ``os.WIFEXITED`` nor ``waitpid(-1)``, so both the
decode and the reaper's exit capture have a Windows-independent path.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


def _spawn_exit(code: int) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", f"raise SystemExit({code})"])
    proc.wait()
    return proc




@pytest.mark.parametrize("code", [0, 7, kb.KANBAN_RATE_LIMIT_EXIT_CODE])
def test_reaper_retains_running_worker_and_records_exit_once(code):
    """The owned Popen survives live ticks, then publishes its exit exactly once."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read(); raise SystemExit(int(sys.argv[1]))", str(code)],
        stdin=subprocess.PIPE,
    )
    try:
        kbd._live_worker_procs[proc.pid] = proc
        assert kbd.reap_worker_zombies() == []
        assert kbd._live_worker_procs[proc.pid] is proc
        assert kbd._classify_worker_exit(proc.pid) == ("unknown", None)

        proc.communicate(timeout=10)
        assert kbd.reap_worker_zombies() == [proc.pid]
        assert proc.pid not in kbd._live_worker_procs
        expected = "clean_exit" if code == 0 else (
            "rate_limited" if code == kb.KANBAN_RATE_LIMIT_EXIT_CODE else "nonzero_exit")
        assert kbd._classify_worker_exit(proc.pid) == (expected, code)
        recorded = kbd._recent_worker_exits[proc.pid]
        assert kbd.reap_worker_zombies() == []
        assert kbd._recent_worker_exits[proc.pid] == recorded
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)


@pytest.mark.platforms("posix")
def test_reaper_records_signaled_worker():
    import signal

    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        kbd._live_worker_procs[proc.pid] = proc
        proc.terminate()
        proc.communicate(timeout=10)
        assert kbd.reap_worker_zombies() == [proc.pid]
        assert kbd._classify_worker_exit(proc.pid) == ("signaled", signal.SIGTERM)
        assert proc.pid not in kbd._live_worker_procs
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)


@pytest.mark.platforms("windows")
def test_native_windows_reaper_and_decode(monkeypatch):
    """Native Windows, nothing patched: ``_IS_WINDOWS`` selects the Popen-poll
    reaper and the decode runs where ``os.WIFEXITED`` does not exist, so the
    rate-limit sentinel exit is a requeue, not a crash."""
    monkeypatch.setattr(kbd, "_live_worker_procs", {})
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})
    assert not hasattr(os, "WIFEXITED")
    proc = _spawn_exit(kb.KANBAN_RATE_LIMIT_EXIT_CODE)
    kbd._live_worker_procs[proc.pid] = proc
    assert kbd.reap_worker_zombies() == [proc.pid]
    assert kbd._classify_worker_exit(proc.pid) == ("rate_limited", kb.KANBAN_RATE_LIMIT_EXIT_CODE)
