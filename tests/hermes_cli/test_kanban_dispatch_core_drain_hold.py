"""``hermes kanban dispatch`` and the dashboard ``POST /dispatch`` honour the core-update drain hold. [daveey]

Only the gateway watcher used to check ``agent.core_update_drain``; ``dispatch_once`` checked just the
gateway's own ``draining`` snapshot. On hermes-hub (2026-11-11 02:10:36Z) a CLI ``hermes kanban dispatch
--json`` claimed run 165540 (lock ``hermes-hub:<cli pid>``) inside hermes-sync's claim-hold while the
gateway itself was holding. These tests drive both non-gateway entry paths against a real board, on the
ready and review lanes, with a live hold (refused) and a dead-writer hold (claims as usual).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import core_update_drain as drain


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _write_hold(home: Path, pid: int) -> None:
    path = home / drain.HOLD_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "started_at": time.time(), "reason": "core update"}))


@pytest.fixture
def board(tmp_path, monkeypatch):
    """Real default board in a scratch home; spawns are recorded, never executed."""
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    (tmp_path / "config.yaml").write_text(_managed_yaml())
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(drain, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(drain, "_canonical_root", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.list_profiles", lambda **kw: [SimpleNamespace(name="default")])
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda name: name == "default")
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "unknown")
    spawned: list[str] = []
    monkeypatch.setattr(kbd, "_default_spawn", lambda task, ws, *, board=None: spawned.append(task.id) or None)
    return SimpleNamespace(home=tmp_path, spawned=spawned)


def _make_card(monkeypatch, lane: str) -> str:
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: lane == "review")
    with kbc.connect_closing(board="default") as conn:
        tid = kb.create_task(conn, title=f"{lane} card", assignee="default")
        if lane == "review":
            from tests.hermes_cli.test_kanban_model_tier import _review_source
            _review_source(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.status == lane and task.claim_lock is None
    return tid


def _status(tid: str) -> tuple[str, object]:
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc

    with kbc.connect_closing(board="default") as conn:
        task = kb.get_task(conn, tid)
        return task.status, task.claim_lock


def _run_cli(monkeypatch, capsys, *, as_json: bool) -> str:
    from hermes_cli import kanban as kb_cli

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {}})
    capsys.readouterr()
    rc = kb_cli._cmd_dispatch(argparse.Namespace(dry_run=False, max=None, failure_limit=2, json=as_json))
    assert rc == 0
    return capsys.readouterr().out


def _run_dashboard() -> dict:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    plugin_file = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_kanban_drain_hold_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    response = TestClient(app).post("/api/plugins/kanban/dispatch?max=4")
    assert response.status_code == 200, response.text
    return response.json()


def _dispatch(entry: str, monkeypatch, capsys) -> dict:
    if entry == "cli":
        return json.loads(_run_cli(monkeypatch, capsys, as_json=True))
    return _run_dashboard()


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("entry", ["cli", "dashboard"])
def test_live_hold_refuses_claim(board, monkeypatch, capsys, entry, lane):
    tid = _make_card(monkeypatch, lane)
    board.spawned.clear()  # exclude the authenticated implementation run
    _write_hold(board.home, os.getpid())
    out = _dispatch(entry, monkeypatch, capsys)
    assert board.spawned == []
    assert _status(tid) == (lane, None)
    assert out["spawned"] == []
    hold = out["core_update_drain_hold"]
    assert hold["pid"] == os.getpid() and hold["path"] == str(board.home / drain.HOLD_RELPATH)
    if entry == "dashboard":  # the tick stops before scheduling, not just at the claim-time recheck
        assert out["scheduling_admitted"] is False and out["lane_candidates"] == 0


@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("entry", ["cli", "dashboard"])
def test_dead_writer_hold_does_not_block(board, monkeypatch, capsys, entry, lane):
    tid = _make_card(monkeypatch, lane)
    board.spawned.clear()  # exclude the authenticated implementation run
    _write_hold(board.home, _dead_pid())
    out = _dispatch(entry, monkeypatch, capsys)
    assert board.spawned == [tid]
    assert _status(tid)[0] == "running"
    assert out["core_update_drain_hold"] is None
    if entry == "dashboard":
        assert out["scheduling_admitted"] is True and out["lane_candidates"] == 1


def test_cli_text_names_the_hold_as_the_reason(board, monkeypatch, capsys):
    _make_card(monkeypatch, "ready")
    _write_hold(board.home, os.getpid())
    out = _run_cli(monkeypatch, capsys, as_json=False)
    assert "Spawned:      0" in out
    assert f"Held: core-update drain (pid {os.getpid()}" in out
    assert board.spawned == []


def test_held_tick_keeps_promotion(board, monkeypatch, capsys):
    """Bookkeeping still runs under the hold (same as the gateway's held tick): a todo whose parent is
    done is promoted, it just is not claimed."""
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc

    monkeypatch.setattr("hermes_cli.kanban_db_dispatch.review_dispatch_enabled", lambda: False)
    with kbc.connect_closing(board="default") as conn:
        parent = kb.create_task(conn, title="parent", assignee="default")
        child = kb.create_task(conn, title="child", assignee="default", parents=[parent])
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    _write_hold(board.home, os.getpid())
    out = json.loads(_run_cli(monkeypatch, capsys, as_json=True))
    assert out["promoted"] >= 1
    assert _status(child) == ("ready", None)
    assert board.spawned == []


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_hold_that_starts_mid_tick_still_refuses_the_claim(board, monkeypatch, capsys, lane):
    """The claim-time recheck (``claim_if_admitting``) sees a hold written after the tick-start check."""
    from hermes_cli import kanban_gateway_drain as kgd

    tid = _make_card(monkeypatch, lane)
    board.spawned.clear()  # exclude the authenticated implementation run
    calls = []
    real = kgd.core_update_hold

    def hold_after_first_check():
        calls.append(1)
        if len(calls) == 2:
            _write_hold(board.home, os.getpid())
        return real()

    monkeypatch.setattr(kgd, "core_update_hold", hold_after_first_check)
    _run_cli(monkeypatch, capsys, as_json=True)
    assert len(calls) >= 2  # tick-start check passed, the claim-time check ran
    assert board.spawned == []
    assert _status(tid) == (lane, None)


def test_unimportable_drain_module_fails_open(board, monkeypatch, capsys):
    """Same rule as the gateway gate: no ``agent.core_update_drain`` means no hold, not a frozen board."""
    tid = _make_card(monkeypatch, "ready")
    _write_hold(board.home, os.getpid())
    monkeypatch.setitem(sys.modules, "agent.core_update_drain", None)  # import raises ImportError
    out = json.loads(_run_cli(monkeypatch, capsys, as_json=True))
    assert out["core_update_drain_hold"] is None
    assert board.spawned == [tid]
