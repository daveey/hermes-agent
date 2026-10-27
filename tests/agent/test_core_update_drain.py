"""The kanban dispatcher spawns nothing while a live hermes-sync core-update drain hold exists."""

import json
import os
import subprocess
import time

import pytest

from agent import core_update_drain as drain


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(drain, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(drain, "_canonical_root", lambda: tmp_path)
    return tmp_path


def _write(home, payload):
    path = home / drain.HOLD_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


def _dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_no_hold_allows_dispatch(home):
    from gateway.kanban_watchers_common import _kanban_dispatch_allowed
    assert drain.active_hold() is None
    assert _kanban_dispatch_allowed() is True


def test_live_hold_blocks_dispatch(home):
    from gateway.kanban_watchers_common import _kanban_dispatch_allowed
    _write(home, {"pid": os.getpid(), "started_at": time.time(), "reason": "core update"})
    assert drain.active_hold()["pid"] == os.getpid()
    assert _kanban_dispatch_allowed() is False


def test_dead_writer_hold_is_ignored(home):
    from gateway.kanban_watchers_common import _kanban_dispatch_allowed
    _write(home, {"pid": _dead_pid(), "started_at": time.time()})
    assert drain.active_hold() is None
    assert _kanban_dispatch_allowed() is True


def test_unparseable_hold_counts_only_briefly(home):
    path = _write(home, "{partial")
    assert drain.active_hold() is not None
    old = time.time() - drain._UNPARSEABLE_GRACE_S - 5
    os.utime(path, (old, old))
    assert drain.active_hold() is None


def test_profile_home_honors_fleet_root_hold(tmp_path, monkeypatch):
    profile = tmp_path / "profiles" / "worker"
    profile.mkdir(parents=True)
    monkeypatch.setattr(drain, "_hermes_home", lambda: profile)
    monkeypatch.setattr(drain, "_canonical_root", lambda: tmp_path)
    _write(tmp_path, {"pid": os.getpid()})
    assert drain.active_hold()["path"] == str(tmp_path / drain.HOLD_RELPATH)


def test_estop_and_drain_are_distinct_gates(home):
    from gateway.kanban_watchers_common import _kanban_dispatch_gate
    assert _kanban_dispatch_gate() is None
    _write(home, {"pid": os.getpid()})
    assert _kanban_dispatch_gate() == "drain"


# ── embedded dispatcher under a live hold: bookkeeping runs, claims don't ─────


def _dispatcher_board(tmp_path, monkeypatch):
    """A board with a due wake probe, a todo card whose parent is done, and a spawnable ready card."""
    from pathlib import Path
    from types import SimpleNamespace

    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_db_schedule as kbs

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr("hermes_cli.profiles.list_profiles", lambda **kw: [SimpleNamespace(name="default")])
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda name: name == "default")
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: False)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "unknown")
    spawned = []
    monkeypatch.setattr(kbd, "_default_spawn", lambda task, ws, *, board=None: spawned.append(task.id) or None)
    with kbc.connect_closing(board="default") as conn:
        parent = kb.create_task(conn, title="parent", assignee="default")
        child = kb.create_task(conn, title="child", assignee="default", parents=[parent])
        ready = kb.create_task(conn, title="ready", assignee="default")
        parked = kb.create_task(conn, title="parked", assignee="default")
        assert kbs.schedule_task(conn, parked, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z wait",
                                 wake_check="true", check_every=120)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        assert kb.get_task(conn, child).status == "todo"
        assert kb.get_task(conn, ready).status == "ready"
    return dict(parent=parent, child=child, ready=ready, parked=parked), spawned


def _run_one_tick(tmp_path, monkeypatch):
    import asyncio

    from gateway import kanban_watchers as watchers
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_wake_check as kwc

    probes = []
    real_probe = kwc.run_due_wake_checks

    def probe(conn, *, now=None, wait=False):
        out = real_probe(conn, now=now, wait=True)
        probes.append(out["submitted"])
        return out

    monkeypatch.setattr(kwc, "run_due_wake_checks", probe)
    runner = watchers.GatewayKanbanWatchersMixin()
    runner._running = True
    cfg = {"auto_decompose": False, "dispatch_interval_seconds": 1}
    monkeypatch.setattr(runner, "_kanban_dispatcher_boot", lambda: (lambda: {"kanban": cfg}, kb, cfg))

    async def no_sleep(delay):
        return None

    async def stop(interval):
        runner._running = False

    monkeypatch.setattr(watchers.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(runner, "_sleep_until_tick_or_sync_wake", stop)
    try:
        asyncio.run(runner._kanban_dispatcher_watcher())
    finally:
        kwc._shutdown_for_tests()
    return probes


def _statuses(ids):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    with kbc.connect_closing(board="default") as conn:
        return {k: kb.get_task(conn, v).status for k, v in ids.items()}


def test_live_hold_runs_wake_probes_and_promotion_but_claims_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(drain, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(drain, "_canonical_root", lambda: tmp_path)
    ids, spawned = _dispatcher_board(tmp_path, monkeypatch)
    _write(tmp_path, {"pid": os.getpid(), "started_at": time.time(), "reason": "core update"})
    probes = _run_one_tick(tmp_path, monkeypatch)
    assert [ids["parked"]] in probes  # the due wake probe ran under the hold
    st = _statuses(ids)
    assert st["parked"] == "ready"  # ... and its condition woke the card
    assert st["child"] == "ready"  # todo with a done parent was promoted
    assert st["ready"] == "ready" and spawned == []  # but nothing was claimed


def test_without_hold_the_same_tick_claims(tmp_path, monkeypatch):
    """Control: the board above is spawnable, so the held tick's zero claims are the hold's doing."""
    monkeypatch.setattr(drain, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(drain, "_canonical_root", lambda: tmp_path)
    ids, spawned = _dispatcher_board(tmp_path, monkeypatch)
    _run_one_tick(tmp_path, monkeypatch)
    assert ids["ready"] in spawned
    assert _statuses(ids)["ready"] == "running"


def test_estop_still_skips_the_whole_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(drain, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(drain, "_canonical_root", lambda: tmp_path)
    ids, spawned = _dispatcher_board(tmp_path, monkeypatch)
    (tmp_path / "ESTOP").touch()
    probes = _run_one_tick(tmp_path, monkeypatch)
    assert probes == [] and spawned == []
    assert _statuses(ids)["child"] == "todo"
