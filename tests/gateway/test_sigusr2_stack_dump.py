"""SIGUSR2 stack dump is a Python-level handler, never faulthandler's C signal-context walk.

faulthandler.register(SIGUSR2, all_threads=True) dumps from the signal handler on whichever thread
caught the signal, walking every other thread's frame chain while those threads keep running. A thread
that pops a frame mid-walk makes dump_frame read freed memory: David-MBP's gateway 43117 segfaulted
that way (faulthandler_user -> _Py_DumpTracebackThreads -> dump_frame -> _PyFrame_SafeGetCode) on
10-30 and took 4 Kanban runs with it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(not hasattr(signal, "SIGUSR2"), reason="POSIX-only signal")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SETUP = """
import faulthandler, os, signal, sys, threading, time
from types import SimpleNamespace
sys.path.insert(0, sys.argv[2])
from gateway.run_startup import GatewayStartupMixin
runner = object.__new__(GatewayStartupMixin)
runner.config = SimpleNamespace(log_dir=sys.argv[1])
runner._start_install_faulthandler()
"""

_STRUCTURAL_CHILD = _SETUP + """
from gateway import run_startup
handler = signal.getsignal(signal.SIGUSR2)
print("unregister", faulthandler.unregister(signal.SIGUSR2))
print("python_handler", isinstance(handler, getattr(run_startup, "ThreadStackDumper", ())))
"""

# 150 threads churning frames (deep recursion + generators) while the parent fires SIGUSR2.
_CHURN_CHILD = _SETUP + """
stop = threading.Event()
go = threading.Event()  # start all threads idle, then release them: starting 150 already-churning
                        # threads one by one is a GIL convoy that takes minutes under load

def gen(n):
    for i in range(n):
        # Yield the CPU with the recursive/generator frames still live, then pop and
        # rebuild them. 150 unbounded GIL contenders otherwise starve the signal/
        # control thread on throttled runners; GIL fairness is not under test.
        time.sleep(0)
        yield i

def recurse(n):
    if n == 0:
        return sum(gen(8))
    return recurse(n - 1) + 1

def churn():
    go.wait()
    while not stop.is_set():
        recurse(40)

threads = [threading.Thread(target=churn, name=f"churn-{i}", daemon=True) for i in range(150)]
for t in threads:
    t.start()
go.set()
print("ready", " ".join(t.name for t in threading.enumerate()), flush=True)
sys.stdin.readline()
stop.set()
# No join: joining 150 GIL-contending threads one by one takes ~30 s under load; they are daemon
# threads, so a normal interpreter exit (rc 0) is the clean-exit proof.
print("clean-exit", flush=True)
"""

_IDLE_CHILD = _SETUP + """
print("ready", flush=True)
sys.stdin.readline()

def run_pending_signal_handlers():
    pass  # entering a Python function checks the eval breaker, so a still-pending SIGUSR2 handler runs here

run_pending_signal_handlers()
"""


def _spawn(code: str, log_dir):
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(log_dir), _REPO_ROOT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert child.stdin and child.stdout and child.stderr
    return child, child.stdin, child.stdout, child.stderr


def _dumps(log_dir) -> list[str]:
    path = os.path.join(str(log_dir), "gateway_faulthandler.log")
    if not os.path.exists(path):
        return []
    text = open(path, encoding="utf-8").read()
    return [b for b in text.split("--- SIGUSR2 stack dump ")[1:]]


def test_setup_installs_python_handler_not_faulthandler_register(tmp_path):
    """Structural: after setup, faulthandler has no SIGUSR2 registration and the signal's
    disposition is the gateway's Python-level ThreadStackDumper."""
    out = subprocess.run(
        [sys.executable, "-c", _STRUCTURAL_CHILD, str(tmp_path), _REPO_ROOT],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "unregister False" in out.stdout, out.stdout
    assert "python_handler True" in out.stdout, out.stdout


def test_sigusr2_storm_against_frame_churning_threads_never_crashes(tmp_path):
    """Behavioral: 300 SIGUSR2 ~5 ms apart against 150 frame-churning threads; the child exits 0
    and the log's last dump names every live thread."""
    child, stdin, stdout, stderr = _spawn(_CHURN_CHILD, tmp_path)
    try:
        line = stdout.readline().split()
        assert line and line[0] == "ready", stderr.read()
        live = line[1:]
        assert len(live) >= 151
        for _ in range(300):
            child.send_signal(signal.SIGUSR2)
            time.sleep(0.005)  # health: allow HX013 -- the ~5 ms signal spacing is the storm under test
        assert child.poll() is None, f"died under SIGUSR2 (rc={child.returncode}): {stderr.read()[-2000:]}"
        stdin.write("stop\n")
        stdin.flush()
        rc = child.wait(timeout=60)
        rest = stdout.read()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    assert rc == 0, f"rc={rc}"
    assert "clean-exit" in rest
    dumps = _dumps(tmp_path)
    assert dumps, "no SIGUSR2 dump written"
    # The rate limit caps 300 signals over ~1.5 s+ to a handful of dumps, not 300.
    assert len(dumps) <= 10, len(dumps)
    last = dumps[-1]
    missing = [name for name in live if f'"{name}"' not in last]
    assert not missing, f"dump lacks threads: {missing[:10]}"
    assert "--- end SIGUSR2 stack dump ---" in last
    assert 'in recurse' in last and 'in gen' in last, "dump missed the live churn frames"


def test_two_sigusr2_one_ms_apart_produce_one_dump(tmp_path):
    child, stdin, stdout, stderr = _spawn(_IDLE_CHILD, tmp_path)
    try:
        assert stdout.readline().strip() == "ready", stderr.read()
        child.send_signal(signal.SIGUSR2)
        time.sleep(0.001)  # health: allow HX013 -- the 1 ms gap between the two signals is the behaviour under test
        child.send_signal(signal.SIGUSR2)
        # Both signals are pending before kill() returns; the child runs their handlers before it
        # exits (on readline's EINTR, or at its explicit Python call after it), so rc 0 settles the count.
        stdin.write("\n")
        stdin.flush()
        assert child.wait(timeout=30) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    assert len(_dumps(tmp_path)) == 1


def test_rate_limit_ignores_second_dump_within_interval_and_allows_after(tmp_path):
    """Direct handler calls: the 1 ms test above could pass on the kernel coalescing the two
    pending signals alone, so drive the rate limit with a fake clock."""
    from gateway.run_startup import ThreadStackDumper

    now = [100.0]
    with open(tmp_path / "log", "a", encoding="utf-8") as fh:
        dumper = ThreadStackDumper(fh, clock=lambda: now[0])
        dumper(signal.SIGUSR2, sys._getframe())
        now[0] += 0.001
        dumper(signal.SIGUSR2, sys._getframe())
        now[0] += 0.5
        dumper(signal.SIGUSR2, sys._getframe())
        assert dumper.dumps == 1
        now[0] += 0.6  # 1.101 s after the first dump
        dumper(signal.SIGUSR2, sys._getframe())
        assert dumper.dumps == 2
    text = (tmp_path / "log").read_text(encoding="utf-8")
    assert text.count("--- SIGUSR2 stack dump ") == 2
    assert "MainThread" in text and "test_rate_limit_ignores_second_dump" in text


def test_dump_failure_never_raises(tmp_path):
    """SIGUSR2 must never terminate the gateway: a write failure (closed fd) is swallowed."""
    from gateway.run_startup import ThreadStackDumper

    fh = open(tmp_path / "log", "a", encoding="utf-8")
    dumper = ThreadStackDumper(fh)
    fh.close()
    dumper(signal.SIGUSR2, None)  # must not raise
