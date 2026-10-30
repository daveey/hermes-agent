"""One exit deadline for a supervised gateway stop.

systemd SIGKILLs the gateway TimeoutStopSec after SIGTERM. stop() had a thread watchdog, but the
work AFTER stop() (cron/housekeeping thread waits in ``_start_gateway_shutdown_tail`` and the
default-executor join inside ``asyncio.run``) had none, so a slow tail was SIGKILLed with no stack
dump and the next boot reported ``previous_unclean_exit`` (defect gateway-a2bcd6fabc).

These tests drive the REAL signal handler, shutdown tail and exit funnel of ``gateway.run`` in a
subprocess (a deadline that ``os._exit``s cannot be tested in-process) with a 6s supervisor budget.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_BUDGET_S = 6.0
# The child must be gone inside the supervisor budget (systemd would SIGKILL at 6s).
_EXIT_WITHIN_S = _BUDGET_S

_CHILD = textwrap.dedent(
    """
    import asyncio, os, signal, sys, threading, time
    sys.path.insert(0, {repo!r})
    from gateway import run as gr
    from gateway.lifecycle_ledger import record_startup

    BLOCK_EXECUTOR = {block_executor!r}

    class Runner:
        _signal_initiated_shutdown = False
        _unplanned_signal = None
        _stop_requested_by_signal = False
        _launchd_exit_timeout_s = None
        _restart_requested = False
        _restart_via_service = False
        should_exit_with_failure = False
        exit_reason = None
        exit_code = None
        config = None
        def __init__(self):
            self._shutdown_event = asyncio.Event()
        async def stop(self):
            self._shutdown_event.set()
        async def wait_for_shutdown(self):
            await self._shutdown_event.wait()

    async def start():
        runner = Runner()
        sig = [False]
        handler = gr._start_gateway_make_shutdown_signal_handler(runner, sig)
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, handler, signal.SIGTERM)
        cron_stop = threading.Event()
        # A cron ticker blocked in a 60s delivery wait that ignores cron_stop.
        cron_thread = threading.Thread(target=time.sleep, args=(60,), daemon=True)
        cron_thread.start()
        housekeeping = threading.Thread(target=cron_stop.wait, daemon=True)
        housekeeping.start()
        pw_stop = threading.Event()
        pw_thread = threading.Thread(target=pw_stop.wait, daemon=True)
        pw_thread.start()
        if BLOCK_EXECUTOR:
            # A routing save stuck on the state.db lock in the default executor: asyncio.run
            # joins it (up to 300s) after start_gateway returns.
            loop.run_in_executor(None, time.sleep, 60)
        print("READY", flush=True)
        await runner.wait_for_shutdown()
        return await gr._start_gateway_shutdown_tail(
            runner, None, cron_stop, None, cron_thread, housekeeping, pw_stop, pw_thread, sig)

    record_startup()
    try:
        ok = asyncio.run(start())
        code = 0 if ok else 1
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    gr._exit_after_graceful_shutdown(code)
    """
)


def _spawn(tmp_path: Path, *, block_executor: bool) -> tuple[subprocess.Popen, Path]:
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    script = tmp_path / "child.py"
    script.write_text(_CHILD.format(repo=str(REPO), block_executor=block_executor))
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env.pop("INVOCATION_ID", None)  # never read the real unit's TimeoutStopSec
    env.update(HERMES_HOME=str(home), HERMES_GATEWAY_EXIT_BUDGET_S=str(_BUDGET_S),
               PYTHONPATH=str(REPO))
    proc = subprocess.Popen([sys.executable, str(script)], env=env, cwd=str(REPO),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        if line.strip() == "READY":
            return proc, home
    proc.kill()
    pytest.fail(f"child never became ready: {proc.stdout.read()}")


def _stop_and_time(proc: subprocess.Popen) -> float:
    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=_EXIT_WITHIN_S + 4)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return float("inf")
    finally:
        # Drain output so the pipe never blocks the child.
        threading.Thread(target=proc.stdout.read, daemon=True).start()
    return time.monotonic() - started


def _ledger(home: Path) -> dict:
    from gateway.lifecycle_ledger import get_lifecycle_sentinel_path
    return json.loads(get_lifecycle_sentinel_path(home).read_text())


def _exit_diag(home: Path) -> list[dict]:
    path = home / "logs" / "gateway-exit-diag.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_wedged_tail_exits_inside_supervisor_budget_with_dump(tmp_path):
    """A tail blocked past the budget (cron wait + executor join) is cut at the exit deadline:
    the process exits before the supervisor's SIGKILL, stamps the ledger and leaves a dump."""
    proc, home = _spawn(tmp_path, block_executor=True)
    elapsed = _stop_and_time(proc)
    assert elapsed < _EXIT_WITHIN_S, f"gateway still alive {elapsed:.1f}s after SIGTERM (budget {_BUDGET_S}s)"
    ledger = _ledger(home)
    assert ledger["phase"] == "exited"
    assert ledger["exit_reason"] == "exit_deadline"
    dump = (home / "logs" / "gateway-shutdown-watchdog.log").read_text()
    assert '"event": "exit_deadline_fired"' in dump
    assert "faulthandler dump" in dump
    records = [r for r in _exit_diag(home) if r.get("tag") == "gateway.exit_deadline"]
    assert records, _exit_diag(home)
    assert records[0]["budget_s"] == _BUDGET_S
    assert "signal" in records[0]["phases"]


def test_slow_cron_wait_is_clamped_and_exit_stays_graceful(tmp_path):
    """Only the cron wait is slow: it is clamped to the time left, so the normal graceful
    exit runs inside the budget and the deadline never has to fire."""
    proc, home = _spawn(tmp_path, block_executor=False)
    elapsed = _stop_and_time(proc)
    assert elapsed < _EXIT_WITHIN_S, f"gateway still alive {elapsed:.1f}s after SIGTERM (budget {_BUDGET_S}s)"
    ledger = _ledger(home)
    assert ledger["exit_reason"] == "graceful_shutdown"
    assert not [r for r in _exit_diag(home) if r.get("tag") == "gateway.exit_deadline"]


def test_routing_save_uses_short_patience_while_exiting(tmp_path, monkeypatch):
    """While the gateway is exiting, a routing save blocked by another state.db writer gives up
    after the short drain patience (not the 20s default) and falls back to sessions.json."""
    import hermes_constants
    import hermes_state
    from gateway import exit_deadline
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource, SessionStore

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: str(tmp_path))
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    entry = store.get_or_create_session(SessionSource(
        platform=Platform.LOCAL, chat_id="cli", chat_name="CLI", chat_type="dm", user_id="u1"))

    holder = sqlite3.connect(str(tmp_path / "state.db"), timeout=0, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(exit_deadline, "is_exiting", lambda: True)
    try:
        started = time.monotonic()
        store.update_session(entry.session_key, last_prompt_tokens=777)
        elapsed = time.monotonic() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        store._db.close()
    # One fast-path attempt + one full-rewrite attempt, each at the drain patience.
    assert elapsed < 3 * exit_deadline.DRAIN_DB_PATIENCE_S + 1.0, elapsed
    mirror = json.loads((tmp_path / "sessions" / "sessions.json").read_text())
    assert mirror[entry.session_key]["last_prompt_tokens"] == 777


def test_budget_resolution_order(monkeypatch):
    from gateway import exit_deadline, shutdown_forensics
    monkeypatch.setattr(shutdown_forensics, "current_systemd_timeout_stop_s", lambda: 70.0)
    monkeypatch.delenv(exit_deadline.BUDGET_ENV, raising=False)
    assert exit_deadline.resolve_supervisor_stop_budget_s() == (70.0, "systemd")
    assert exit_deadline.resolve_supervisor_stop_budget_s(60) == (60.0, "launchd")
    monkeypatch.setenv(exit_deadline.BUDGET_ENV, "9")
    assert exit_deadline.resolve_supervisor_stop_budget_s(60) == (9.0, "env")
    assert exit_deadline.margin_for(70) == exit_deadline.EXIT_DEADLINE_MARGIN_S
    assert exit_deadline.margin_for(6) == 1.5


def test_arm_is_skipped_for_in_process_pytest_stops(monkeypatch):
    """stop()-driving unit tests must never get a delayed os._exit in the pytest worker."""
    from gateway import exit_deadline
    monkeypatch.delenv(exit_deadline.BUDGET_ENV, raising=False)
    exit_deadline._reset_for_tests()
    try:
        assert exit_deadline.arm("stop") is False
        assert exit_deadline.is_exiting() is False
        assert exit_deadline.clamp_wait(65.0, 7.0) == 65.0
    finally:
        exit_deadline._reset_for_tests()
