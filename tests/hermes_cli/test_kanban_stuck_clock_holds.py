"""The "dispatcher stuck" warning knows about ready cards held on a known clock. [daveey]

hermes-hub 2026-11-01 23:48Z: 43 ready cards waited on a Codex quota wall that resets 1793774148, and the
dispatcher logged ``kanban dispatcher stuck ... subscription_quota_reset=43`` every 5 minutes with no reset time.
A card held until a known deadline is waiting, not stuck:

* the held-back breakdown names the deadline: ``subscription_quota_reset=43 (until 2026-11-03T22:35:48Z)``;
* when EVERY ready card of every ticked board waits on a known clock, the stuck warning does not fire
  (CLI ``daemon --force`` loop and the embedded gateway dispatcher both).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

STUCK = "kanban dispatcher stuck"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(int(epoch), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli.kanban_model_assignment import _assignee_config
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    (home / "config.yaml").write_text(_managed_yaml())
    config = _assignee_config("default")
    assert config is not None and config["model_routing"]["enabled"]
    monkeypatch.setattr("hermes_cli.profiles.list_profiles", lambda **kw: [SimpleNamespace(name="default")])
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda name: name == "default")
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: False)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda *a, **kw: "unknown")
    kb.init_db()
    return home


def _quota_held(conn, title: str, resets_at: float) -> str:
    """A ready card whose latest run hit a subscription quota wall that resets at ``resets_at``."""
    now = int(time.time())
    tid = kb.create_task(conn, title=title, assignee="default")
    with kb.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, started_at, ended_at, outcome, status) "
            "VALUES (?, 'default', ?, ?, 'rate_limited', 'rate_limited')", (tid, now - 120, now - 60))
        kb._append_event(conn, tid, "worker_result", {
            "exit_code": kb.KANBAN_RATE_LIMIT_EXIT_CODE, "failure_reason": "rate_limit",
            "failure_retryable": True, "failure_resets_at": resets_at}, run_id=cur.lastrowid)
    return tid


def _cooldown_held(conn, title: str) -> tuple[str, int]:
    """A ready card whose latest run was rate-limited with no typed reset: held by the requeue cooldown."""
    now = int(time.time())
    tid = kb.create_task(conn, title=title, assignee="default")
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, started_at, ended_at, outcome, status) "
            "VALUES (?, 'default', ?, ?, 'rate_limited', 'rate_limited')", (tid, now - 60, now - 30))
    return tid, now - 30 + kb._resolve_rate_limit_cooldown_seconds()


def _auth_held(conn, title: str) -> str:
    """A ready card held by ``blocker_auth``: no clock ends it, a human has to."""
    tid = kb.create_task(conn, title=title, assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET last_failure_error = 'HTTP 403 forbidden: bad credentials' WHERE id = ?",
                     (tid,))
    return tid


def _tick(conn, spawned: list) -> kbd.DispatchResult:
    return kbd.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or 42)


# --- warning content -------------------------------------------------------------------------------------------


def test_breakdown_names_the_quota_reset_deadline(home):
    soon, later = time.time() + 7200, time.time() + 2 * 86400
    spawned: list = []
    with kbc.connect() as conn:
        _quota_held(conn, "later", later)
        _quota_held(conn, "soon", soon)
        res = _tick(conn, spawned)
    assert spawned == []
    held = kbd.describe_suppression([res])
    # Earliest deadline first: it is when the next card can move.
    assert f"subscription_quota_reset=2 (until {_iso(soon)}..{_iso(later)})" in held
    assert f"subscription_quota_reset=2 (until {_iso(soon)}..{_iso(later)})" in kbd.stuck_warning(6, [res])


def test_single_deadline_and_cooldown_are_named(home):
    reset = time.time() + 3600
    with kbc.connect() as conn:
        _quota_held(conn, "walled", reset)
        _tid, cooldown_until = _cooldown_held(conn, "cooling")
        res = _tick(conn, [])
    held = kbd.describe_suppression([res])
    assert f"subscription_quota_reset=1 (until {_iso(reset)})" in held
    assert f"rate_limit_cooldown=1 (until {_iso(cooldown_until)})" in held


def test_non_clock_guard_has_no_deadline(home):
    with kbc.connect() as conn:
        _auth_held(conn, "auth")
        res = _tick(conn, [])
    assert "blocker_auth=1" in kbd.describe_suppression([res])
    assert "until" not in kbd.describe_suppression([res])


# --- when it fires ---------------------------------------------------------------------------------------------


def test_all_clock_held_only_when_every_ready_card_waits_on_a_clock(home):
    with kbc.connect() as conn:
        _quota_held(conn, "a", time.time() + 3600)
        _cooldown_held(conn, "b")
        all_held = _tick(conn, [])
        assert kbd.all_clock_held([all_held])
        _auth_held(conn, "c")
        mixed = _tick(conn, [])
    assert not kbd.all_clock_held([mixed])


def test_all_clock_held_edge_cases():
    held = kbd.DispatchResult(lane_candidates=1)
    held.clock_held["t_a"] = ("subscription_quota_reset", 1793774148)
    locked = kbd.DispatchResult(skipped_locked=True)
    owned = kbd.DispatchResult(skipped_owned=True)
    empty = kbd.DispatchResult()
    assert kbd.all_clock_held([held, None, locked, owned, empty]), "other boards have nothing waiting"
    assert not kbd.all_clock_held([]), "no board ticked"
    assert not kbd.all_clock_held([locked, owned]), "no board actually ticked"
    assert not kbd.all_clock_held([empty]), "nothing is held on a clock: the ready queue is elsewhere"
    spawned = kbd.DispatchResult(lane_candidates=2)
    spawned.clock_held["t_a"] = ("subscription_quota_reset", 1793774148)
    spawned.spawned.append(("t_b", "default", ""))
    assert not kbd.all_clock_held([spawned])
    partial = kbd.DispatchResult(lane_candidates=2)
    partial.clock_held["t_a"] = ("subscription_quota_reset", 1793774148)
    assert not kbd.all_clock_held([partial])


# --- CLI daemon entry path -------------------------------------------------------------------------------------


def _daemon_stderr(monkeypatch, capsys, res, ticks=8):
    from hermes_cli import kanban_ops

    monkeypatch.setattr(kbd, "has_spawnable_ready", lambda conn: True)

    def fake_run_daemon(*, on_tick, **_kw):
        for _ in range(ticks):
            on_tick(res)

    monkeypatch.setattr(kbd, "run_daemon", fake_run_daemon)
    assert kanban_ops._cmd_daemon(argparse.Namespace(force=True, interval=1, max=None, pidfile=None)) == 0
    return capsys.readouterr().err


def test_cli_daemon_silent_when_every_ready_card_waits_on_quota_reset(home, monkeypatch, capsys):
    with kbc.connect() as conn:
        _quota_held(conn, "a", time.time() + 3600)
        _quota_held(conn, "b", time.time() + 7200)
        res = _tick(conn, [])
    assert STUCK not in _daemon_stderr(monkeypatch, capsys, res)


def test_cli_daemon_warns_with_deadline_when_other_cards_are_held(home, monkeypatch, capsys):
    reset = time.time() + 3600
    with kbc.connect() as conn:
        _quota_held(conn, "a", reset)
        _auth_held(conn, "b")
        res = _tick(conn, [])
    err = _daemon_stderr(monkeypatch, capsys, res)
    assert STUCK in err
    assert f"subscription_quota_reset=1 (until {_iso(reset)})" in err


# --- embedded gateway dispatcher entry path --------------------------------------------------------------------


def _run_gateway_ticks(monkeypatch, n):
    from gateway import kanban_watchers as watchers
    from hermes_cli import kanban_wake_check as kwc

    spawned: list = []
    monkeypatch.setattr(kbd, "_default_spawn", lambda task, ws, *, board=None: spawned.append(task.id) or None)
    runner = watchers.GatewayKanbanWatchersMixin()
    runner._running = True
    runner._external_drain_active = False
    runner._restart_requested = False
    cfg = {"auto_decompose": False, "dispatch_interval_seconds": 1}
    monkeypatch.setattr(runner, "_kanban_dispatcher_boot", lambda: (lambda: {"kanban": cfg}, kb, cfg))
    tick = {"n": 0}

    async def no_sleep(delay):
        return None

    async def next_tick(interval):
        tick["n"] += 1
        if tick["n"] >= n:
            runner._running = False

    monkeypatch.setattr(watchers.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(runner, "_sleep_until_tick_or_sync_wake", next_tick)
    try:
        asyncio.run(runner._kanban_dispatcher_watcher())
    finally:
        kwc._shutdown_for_tests()
    return spawned


def _stuck_lines(caplog):
    return [r.getMessage() for r in caplog.records if STUCK in r.getMessage()]


def test_gateway_silent_when_every_ready_card_waits_on_quota_reset(home, monkeypatch, caplog):
    from gateway import kanban_watchers as watchers

    with kbc.connect_closing(board="default") as conn:
        _quota_held(conn, "a", time.time() + 3600)
        _quota_held(conn, "b", time.time() + 7200)
    caplog.set_level(logging.INFO, logger="gateway")
    assert _run_gateway_ticks(monkeypatch, watchers._HEALTH_WINDOW + 4) == []
    assert _stuck_lines(caplog) == []


def test_gateway_warns_with_deadline_when_other_cards_are_held(home, monkeypatch, caplog):
    from gateway import kanban_watchers as watchers

    reset = time.time() + 3600
    with kbc.connect_closing(board="default") as conn:
        _quota_held(conn, "a", reset)
        _auth_held(conn, "b")
    caplog.set_level(logging.INFO, logger="gateway")
    assert _run_gateway_ticks(monkeypatch, watchers._HEALTH_WINDOW + 4) == []
    lines = _stuck_lines(caplog)
    assert lines, "a card held by blocker_auth is not on a clock"
    assert f"subscription_quota_reset=1 (until {_iso(reset)})" in lines[0]
