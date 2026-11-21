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


@pytest.mark.parametrize("age,active", [(119, True), (120, False), (1800, False)])
def test_live_writer_lease_expires(home, monkeypatch, age, active):
    from gateway.kanban_watchers_common import _kanban_dispatch_allowed

    record = {"pid": os.getpid(), "started_at": 1000, "max_s": 120}
    path = _write(home, record)
    monkeypatch.setattr(drain.time, "time", lambda: 1000 + age)
    assert drain.active_hold() == ({**record, "path": str(path)} if active else None)
    assert _kanban_dispatch_allowed() is not active
    assert path.exists()  # a reader never deletes another publisher's marker


@pytest.mark.parametrize("fields", [
    {"max_s": None}, {"max_s": "bad"}, {"max_s": float("inf")},
    {"max_s": float("nan")}, {"max_s": -1}, {"max_s": 0},
    {"max_s": 120, "started_at": "bad"},
    {"max_s": 120, "started_at": float("inf")},
    {"max_s": 120, "started_at": float("nan")},
])
def test_invalid_lease_cannot_hold_dispatch_forever(home, fields):
    path = _write(home, {"pid": os.getpid(), "started_at": 1000, **fields})
    assert drain.read_hold(path, now=0) is None


def test_expired_profile_hold_does_not_hide_active_root(tmp_path, monkeypatch):
    profile = tmp_path / "profiles" / "worker"
    monkeypatch.setattr(drain, "_hermes_home", lambda: profile)
    monkeypatch.setattr(drain, "_canonical_root", lambda: tmp_path)
    monkeypatch.setattr(drain.time, "time", lambda: 2000)
    _write(profile, {"pid": os.getpid(), "started_at": 1000, "max_s": 120})
    root = _write(tmp_path, {"pid": os.getpid(), "started_at": 1999, "max_s": 120})
    assert drain.active_hold()["path"] == str(root)


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
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    (tmp_path / "config.yaml").write_text(_managed_yaml())
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


def _run_one_tick(tmp_path, monkeypatch, *, restart_requested=False):
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
    monkeypatch.setattr(runner, "_restart_requested", restart_requested, raising=False)
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


@pytest.mark.parametrize("expired_hold", [False, True])
def test_without_active_hold_the_same_tick_claims(tmp_path, monkeypatch, expired_hold):
    """A spawnable board resumes claims even while the expired hold's writer lives."""
    monkeypatch.setattr(drain, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(drain, "_canonical_root", lambda: tmp_path)
    ids, spawned = _dispatcher_board(tmp_path, monkeypatch)
    if expired_hold:
        _write(tmp_path, {"pid": os.getpid(), "started_at": time.time() - 1800, "max_s": 120})
    _run_one_tick(tmp_path, monkeypatch)
    assert ids["ready"] in spawned
    assert _statuses(ids)["ready"] == "running"


def test_a_deferred_restart_claims_nothing_but_keeps_wake_probes_and_promotion(tmp_path, monkeypatch):
    """With no hermes-sync hold (core declares GATEWAY_RESTART_SAFE), the gateway's own deferred restart
    must not claim: each claim would add a run the after-turn wait has to carry. [daveey]"""
    monkeypatch.setattr(drain, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(drain, "_canonical_root", lambda: tmp_path)
    ids, spawned = _dispatcher_board(tmp_path, monkeypatch)
    probes = _run_one_tick(tmp_path, monkeypatch, restart_requested=True)
    assert [ids["parked"]] in probes
    st = _statuses(ids)
    assert st["parked"] == "ready" and st["child"] == "ready"
    assert st["ready"] == "ready" and spawned == []


def test_estop_still_skips_the_whole_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(drain, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(drain, "_canonical_root", lambda: tmp_path)
    ids, spawned = _dispatcher_board(tmp_path, monkeypatch)
    (tmp_path / "ESTOP").touch()
    probes = _run_one_tick(tmp_path, monkeypatch)
    assert probes == [] and spawned == []
    assert _statuses(ids)["child"] == "todo"


def test_hold_requeues_a_dead_worker_and_does_not_claim_until_the_hold_lifts(tmp_path, monkeypatch):
    """A worker that dies during a drain hold goes back to ready and is not claimed again while the hold is up."""
    import subprocess
    import sys

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    (tmp_path / "config.yaml").write_text(_managed_yaml())
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(drain, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(drain, "_canonical_root", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda name: name == "default")
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: False)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "unknown")

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    spawned = []

    def spawn(task, ws, board=None):
        spawned.append(task.id)
        return 1

    with kbc.connect_closing(board="default") as conn:
        tid = kb.create_task(conn, title="died-during-drain", assignee="default")
        kb.claim_task(conn, tid)
        kbd._set_worker_pid(conn, tid, child.pid)
        aged = int(time.time()) - 3600
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (aged, tid))
        _write(tmp_path, {"pid": os.getpid(), "started_at": time.time(), "reason": "core update"})

        held = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=0)
        task = kb.get_task(conn, tid)
        assert task.status == "ready", (task.status, held.crashed)
        assert task.worker_pid is None
        assert spawned == []

        spawned.clear()
        kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=0)
        assert kb.get_task(conn, tid).status == "ready"
        assert spawned == []

        (tmp_path / drain.HOLD_RELPATH).unlink()
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert spawned == [tid]
        assert kb.get_task(conn, tid).status == "running"
