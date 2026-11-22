"""SessionDB.__del__ must never wait for sqlite_safe_read._live_lock.

CI hung 600 s after a passing test: the main thread sat in __del__ -> close ->
_TrackingMixin.close -> ``with _live_lock`` while another thread owned the lock
(a daemon frozen at interpreter teardown can own it forever).
"""

import subprocess
import sys
import textwrap
import threading
from pathlib import Path

from hermes_cli import sqlite_safe_read as safe
from hermes_state import SessionDB


def _hold_live_lock():
    held, release = threading.Event(), threading.Event()

    def holder():
        with safe._live_lock:
            held.set()
            release.wait(30)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(10)
    return release, thread


def test_finalizer_does_not_wait_for_live_lock_held_by_another_thread(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    path = db.db_path
    release, holder = _hold_live_lock()
    finished = threading.Event()

    def finalize():
        db.__del__()
        finished.set()

    runner = threading.Thread(target=finalize, daemon=True)
    runner.start()
    try:
        assert finished.wait(5), "SessionDB.__del__ blocked on _live_lock"
    finally:
        release.set()
        holder.join(10)
        runner.join(10)
    # The deferred close lands on the next registry operation.
    assert safe.has_live_connection(path) is False


def test_explicit_close_still_waits_for_live_lock(tmp_path):
    """Only finalizers skip the wait: close() keeps its \"unregister under the lock\" contract."""
    db = SessionDB(tmp_path / "state.db")
    release, holder = _hold_live_lock()
    finished = threading.Event()

    def close():
        db.close()
        finished.set()

    runner = threading.Thread(target=close, daemon=True)
    runner.start()
    try:
        assert not finished.wait(0.5)
    finally:
        release.set()
        holder.join(10)
    assert finished.wait(10)
    runner.join(10)


_EXIT_PROBE = """
import sys, threading
from pathlib import Path
from hermes_cli import sqlite_safe_read as safe
import hermes_state

import types
holder = types.ModuleType('hermes_probe_holder')  # cleared with sys.modules at teardown, so __del__ runs then
holder.db = hermes_state.SessionDB(Path(sys.argv[1]) / 'state.db')
sys.modules['hermes_probe_holder'] = holder
del holder
held = threading.Event()
def hold():
    with safe._live_lock:
        held.set()
        threading.Event().wait()
threading.Thread(target=hold, daemon=True).start()
assert held.wait(10)
print('main-returning', flush=True)
"""


def test_interpreter_exit_does_not_hang_when_a_daemon_owns_live_lock(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_EXIT_PROBE), str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    assert "main-returning" in result.stdout, result.stderr
    assert result.returncode == 0, result.stderr
