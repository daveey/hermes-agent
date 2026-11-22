"""Regression test for #28181 — kanban worker SIGTERM must terminate the process.

The single-query signal handler in cli.py (``_signal_handler_q``) raises
``KeyboardInterrupt`` to unwind the main thread on SIGTERM/SIGHUP. That works
for interactive ``hermes chat -q`` invocations, but kanban workers spawned by
the dispatcher are likely to have a non-daemon thread alive (terminal_tool's
``_wait_for_process``, custom plugin background workers, etc.). With
``KeyboardInterrupt`` only the main thread unwinds; the non-daemon thread
keeps the process alive after the gateway has already restarted, the kanban
dispatcher's ``_pid_alive`` check returns True forever, and the task stays
``running`` indefinitely.

The fix: when the process is a dispatcher-spawned worker (``HERMES_KANBAN_TASK``
env var set), flush logging + stdout/stderr and call ``os._exit(0)`` instead.
The kernel reclaims the PID immediately, and ``detect_crashed_workers``
reclaims the stale claim on the next dispatcher tick.

These tests use a synthetic Python script that mirrors the cli.py signal
handler shape so we can exercise the exit-path contract without booting the
full CLI (which needs a real provider config).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

def _synthetic_worker_script() -> str:
    """A standalone script that mirrors cli.py's single-query SIGTERM handler.

    Keeping the synthetic copy here means the test exercises the exact handler
    shape without needing the full hermes_cli boot path (config, providers,
    skills, etc.). If the production handler in cli.py drifts, the test
    that loads the real handler (test_real_handler_uses_os_exit) will catch it.
    """
    return textwrap.dedent(
        """
        import os, signal, sys, threading, time

        # Non-daemon thread that blocks forever — simulates the worker
        # thread that would prevent orderly Python shutdown after
        # KeyboardInterrupt unwinds main.
        stuck = threading.Event()
        threading.Thread(target=stuck.wait, daemon=False).start()

        # Bound this child's life to the test process: if the parent dies
        # (SIGKILL, timeout, reaped slot) stdin hits EOF / we get reparented,
        # and we exit instead of leaking forever as an orphan.
        parent = os.getppid()

        def _watchdog():
            try:
                sys.stdin.buffer.read()
            except Exception:
                pass
            os._exit(1)

        def _ppid_watch():
            while os.getppid() == parent:
                time.sleep(0.2)
            os._exit(1)

        threading.Thread(target=_watchdog, daemon=True).start()
        threading.Thread(target=_ppid_watch, daemon=True).start()

        def handler(signum, frame):
            # Mirrors cli.py:_signal_handler_q. Real handler sleeps 1.5s; the
            # test uses a short grace so it runs fast.
            try:
                time.sleep(0.05)
            except Exception:
                pass
            if os.environ.get("HERMES_KANBAN_TASK"):
                try:
                    if hasattr(signal, "SIGALRM"):
                        signal.signal(signal.SIGALRM, lambda *_: os._exit(0))
                        signal.alarm(2)
                except Exception:
                    pass
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(0)
            raise KeyboardInterrupt()

        signal.signal(signal.SIGTERM, handler)

        # Direct delivery to a helper makes the pending-main-thread boundary
        # reproducible, without depending on the kernel's process-signal choice.
        request = os.environ.get("HERMES_TEST_SIGNAL_REQUEST")
        if request:
            def deliver_from_helper():
                while not os.path.exists(request):
                    time.sleep(0.01)
                signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
            threading.Thread(target=deliver_from_helper, daemon=True).start()

        print("READY", flush=True)
        try:
            # Python handlers run only in the main thread, even when the OS
            # delivers the signal to a helper. An unbounded lock wait need not
            # be interrupted in that case; periodically re-enter the evaluator.
            idle = threading.Event()
            while not idle.wait(0.05):
                pass
        except KeyboardInterrupt:
            sys.exit(0)
        """
    )

def _is_alive_like_dispatcher(pid: int) -> bool:
    """Mirrors hermes_cli/kanban_db.py:_pid_alive on Linux.

    A zombie is treated as dead — the dispatcher's _pid_alive checks
    /proc/<pid>/status for State: Z. We replicate that here so a clean
    os._exit followed by zombie-state is correctly counted as dead.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform == "linux":
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("State:"):
                        if "Z" in line.split(":", 1)[1]:
                            return False
                        break
        except (FileNotFoundError, PermissionError, OSError):
            pass
    elif sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1,
                check=False,
            )
            if proc.returncode != 0:
                return False
            if "Z" in (proc.stdout or "").strip():
                return False
        except (OSError, subprocess.SubprocessError, TimeoutError):
            pass
    return True

def _spawn_synthetic(env_overrides: dict) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(env_overrides)
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", _synthetic_worker_script()],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    # Wait for "READY" so we know the signal handler is installed.
    assert proc.stdout is not None
    deadline = time.time() + 5.0
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line and line.startswith(b"READY"):
            return proc
    proc.kill()
    raise RuntimeError("synthetic worker never signalled READY")

def _cleanup(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except OSError:
            pass
    try:
        proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGTERM semantics differ on Windows; kanban dispatcher is POSIX-only",
)
@pytest.mark.parametrize("signal_route", ["process", "helper-thread"])
def test_sigterm_with_kanban_task_env_terminates_quickly(tmp_path, signal_route):
    """With HERMES_KANBAN_TASK set, SIGTERM should kill the process in <2s
    even when a non-daemon thread is still alive."""
    request = tmp_path / "signal-request"
    env = {"HERMES_KANBAN_TASK": "t_test_28181"}
    if signal_route == "helper-thread":
        if not hasattr(signal, "pthread_kill"):
            pytest.skip("thread-directed POSIX signals unavailable")
        env["HERMES_TEST_SIGNAL_REQUEST"] = str(request)
    proc = _spawn_synthetic(env)
    try:
        t0 = time.time()
        if signal_route == "helper-thread":
            request.touch()
        else:
            os.kill(proc.pid, signal.SIGTERM)

        # Should die in <2s. The handler sleeps ~50ms, then os._exit(0)
        # is immediate. Give generous headroom for slow CI runners.
        deadline = t0 + 2.0
        while time.time() < deadline:
            if not _is_alive_like_dispatcher(proc.pid):
                elapsed = time.time() - t0
                assert elapsed < 2.0
                return
            time.sleep(0.02)
        pytest.fail(
            "process still alive 2s after SIGTERM with HERMES_KANBAN_TASK set "
            "(dispatcher would keep extending claim) — fix regressed"
        )
    finally:
        _cleanup(proc)


# bypass: the orphaned child leaves the guard's process subtree once its
# parent is SIGKILLed, and the cleanup must still be able to kill it.
@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="kanban dispatcher is POSIX-only",
)
def test_synthetic_child_exits_when_parent_is_sigkilled():
    """The stuck-thread child must not outlive a test process that dies
    before it can send SIGTERM (run_tests.sh timeout, killed xdist worker)."""
    parent_code = textwrap.dedent(
        f"""
        import importlib.util, sys, time
        spec = importlib.util.spec_from_file_location("m", {__file__!r})
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        child = m._spawn_synthetic({{"HERMES_KANBAN_TASK": "t_test_28181"}})
        print(child.pid, flush=True)
        time.sleep(60)
        """
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child_pid = 0
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline().strip())
        assert _is_alive_like_dispatcher(child_pid)

        parent.kill()
        parent.wait(timeout=5)

        deadline = time.time() + 5.0
        while time.time() < deadline:
            if not _is_alive_like_dispatcher(child_pid):
                return
            time.sleep(0.05)
        pytest.fail("synthetic child still alive 5s after its parent was SIGKILLed")
    finally:
        if child_pid:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        parent.kill()
        parent.communicate()
