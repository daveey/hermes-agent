"""[daveey] The signed-bundle launchd wrapper must outlive a draining gateway on a stop signal.

launchd treats the job's main process (the wrapper) as the job: if the wrapper dies at the first SIGTERM while
the gateway still drains, launchd thinks the job ended and starts a new gateway into a host the old one still
owns (start_refused owner_alive=true, defect gateway-cc28b19bca). These run on any POSIX host: the wrapper is
plain Python, only its interpreter differs on macOS.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gateway import status
from hermes_cli.gateway_launchd import _SIGNED_WRAPPER_SOURCE, launchd_program_arguments

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX signals")

# A gateway stand-in: records each signal it gets, drains for DRAIN_S after the first SIGTERM, exits 7.
_DRAINING_CHILD = r"""
import os, signal, sys, time
log = sys.argv[1]
def note(n, _f):
    with open(log, "a") as fh:
        fh.write(signal.Signals(n).name + "\n")
    if n == signal.SIGTERM and not getattr(note, "draining", False):
        note.draining = True
for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGUSR1, signal.SIGUSR2):
    signal.signal(s, note)
with open(log, "a") as fh:
    fh.write("READY %d\n" % os.getpid())
while not getattr(note, "draining", False):
    time.sleep(0.02)
time.sleep(float(os.environ.get("DRAIN_S", "1.5")))
sys.exit(7)
"""


def _start(tmp_path: Path):
    child_py = tmp_path / "child.py"
    child_py.write_text(_DRAINING_CHILD)
    sig_log = tmp_path / "signals.log"
    command = [sys.executable, str(child_py), str(sig_log)]
    wrapper = subprocess.Popen(
        launchd_program_arguments(command, tmp_path / "out.log", tmp_path / "err.log",
                                  responsible=Path(sys.executable)),
        start_new_session=True,  # its own group leader, as launchd makes it
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if sig_log.exists() and "READY" in sig_log.read_text():
            break
        time.sleep(0.02)
    else:
        os.killpg(wrapper.pid, signal.SIGKILL)
        raise AssertionError("child never became ready: " + (tmp_path / "err.log").read_text())
    return wrapper, sig_log


def _signals(sig_log: Path) -> list[str]:
    return [ln for ln in sig_log.read_text().splitlines() if not ln.startswith("READY")]


def _finish(wrapper) -> int:
    try:
        return wrapper.wait(timeout=10)
    finally:
        if wrapper.poll() is None:
            os.killpg(wrapper.pid, signal.SIGKILL)
            wrapper.wait()


def test_wrapper_outlives_draining_child_on_sigterm_and_returns_its_status(tmp_path):
    """SIGTERM to the wrapper alone (launchd's main pid): the wrapper forwards it, stays alive for the whole
    drain, and exits with the child's status, so launchd sees the job end only when the gateway has."""
    wrapper, sig_log = _start(tmp_path)
    t0 = time.monotonic()
    os.kill(wrapper.pid, signal.SIGTERM)
    time.sleep(0.7)  # health: allow HX013 -- the wrapper must still be alive mid-drain: the delay is the behaviour
    assert wrapper.poll() is None, "wrapper died before its draining child (launchd would restart into it)"
    rc = _finish(wrapper)
    assert rc == 7
    assert time.monotonic() - t0 >= 1.4
    assert _signals(sig_log) == ["SIGTERM"]


@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGUSR1, signal.SIGUSR2])
def test_wrapper_forwards_other_lifecycle_signals_and_keeps_waiting(tmp_path, sig):
    wrapper, sig_log = _start(tmp_path)
    os.kill(wrapper.pid, sig)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not _signals(sig_log):
        time.sleep(0.02)
    assert _signals(sig_log) == [sig.name]
    assert wrapper.poll() is None
    os.kill(wrapper.pid, signal.SIGTERM)
    assert _finish(wrapper) == 7


def test_wrapper_group_stop_still_waits_for_child(tmp_path):
    """A process-group SIGTERM (bootout of the whole job) reaches the child directly and via the wrapper;
    the wrapper still waits and returns the child's status."""
    wrapper, sig_log = _start(tmp_path)
    os.killpg(wrapper.pid, signal.SIGTERM)
    time.sleep(0.7)  # health: allow HX013 -- the wrapper must still be alive mid-drain: the delay is the behaviour
    assert wrapper.poll() is None
    assert _finish(wrapper) == 7
    assert set(_signals(sig_log)) == {"SIGTERM"}


def test_wrapper_maps_child_signal_death_to_128_plus_n(tmp_path):
    command = [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]
    wrapper = subprocess.Popen(
        launchd_program_arguments(command, tmp_path / "o", tmp_path / "e", responsible=Path(sys.executable)),
        start_new_session=True)
    assert _finish(wrapper) == 128 + 9


def test_wrapper_source_stays_one_ps_line_and_keeps_status_marker():
    """gateway.status recognizes the wrapper by this marker on one ps line; a newline would split it."""
    assert "\n" not in _SIGNED_WRAPPER_SOURCE
    assert status.SIGNED_LAUNCHD_WRAPPER_MARKER in _SIGNED_WRAPPER_SOURCE
