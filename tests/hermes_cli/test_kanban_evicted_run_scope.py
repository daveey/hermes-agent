"""Evicting a foreign copy must not discard the proof needed to reap its scope."""
import json
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_node as wn
from hermes_cli import kanban_worker_scope as kws
from tests.hermes_cli import test_kanban_worker_node as node_tests
from tests.hermes_cli.test_kanban_run_scope_stop import FakeSystemd, _dead_pid, _start


@pytest.fixture
def svc():
    return node_tests.svc.__wrapped__()


@pytest.fixture
def gws(tmp_path, monkeypatch, svc):
    yield from node_tests.gws.__wrapped__(tmp_path, monkeypatch, svc)


def _placed_run(gws):
    home, worker = gws
    tid = node_tests._hub_card(home, runs_on=["machine:worker-1"])
    worker.round()
    worker.enter()
    dead = _dead_pid()
    kbd.dispatch_once(worker.conn, spawn_fn=lambda *a, **k: dead, board="default")
    run = kb._current_run_id(worker.conn, tid)
    return tid, run, f"hermes-worker-kanban-{tid}-run-{run}.scope"


@pytest.mark.parametrize("persist", [False, True])
def test_evicted_ended_run_reaped_after_grace(gws, tmp_path, monkeypatch, persist):
    home, worker = gws
    systemd = FakeSystemd(tmp_path, monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    tid, run, unit = _placed_run(gws)
    with subprocess.Popen(["sleep", "3600"]) as child:
        try:
            systemd.add(unit, [child.pid])
            if persist:
                (worker.home / "processes.json").write_text(json.dumps([
                    {"pid": child.pid, "host_start_time": _start(child.pid), "persist_on_release": True}
                ]))
            assert kb.complete_task(worker.conn, tid, result="done", expected_run_id=run)
            ended = worker.conn.execute("SELECT ended_at FROM task_runs WHERE id=?", (run,)).fetchone()[0]
            worker.round()
            home.round()
            worker.enter()
            wn.evict(worker.conn)
            assert kb.get_task(worker.conn, tid) is None
            assert worker.conn.execute("SELECT 1 FROM task_runs WHERE id=? AND task_id=?", (run, tid)).fetchone() is None
            assert kws.stop_ended_run_scopes(worker.conn, force=True) == []
            monkeypatch.setattr(time, "time", lambda: ended + kws.RUN_SCOPE_STOP_GRACE_SECONDS + 1)
            records = kws.stop_ended_run_scopes(worker.conn, force=True)
            assert systemd.stops == ([] if persist else [unit])
            assert [r["systemd_unit"] for r in records] == ([] if persist else [unit])
            assert child.poll() is None  # FakeSystemd records stops; never touches host scopes.
        finally:
            child.terminate()
            child.wait(timeout=10)


def test_open_home_run_with_live_lease_is_not_stopped(gws, svc, tmp_path, monkeypatch):
    home, worker = gws
    systemd = FakeSystemd(tmp_path, monkeypatch)
    tid, _run, unit = _placed_run(gws)
    worker.round()
    home.round()
    assert svc.board("default")["leases"][tid]["expires"] > time.time() * 1000
    assert home.conn.execute("SELECT ended_at FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] is None
    worker.enter()
    assert tid not in wn.evict(worker.conn)
    with subprocess.Popen(["sleep", "3600"]) as child:
        try:
            systemd.add(unit, [child.pid])
            assert kws.stop_ended_run_scopes(worker.conn, grace_seconds=0, force=True) == []
            assert systemd.stops == []
            assert child.poll() is None
        finally:
            child.terminate()
            child.wait(timeout=10)
