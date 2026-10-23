"""Condition wakes: a scheduled card wakes when a cheap ``wake_check`` probe passes,
not only when its clock runs out.

Regression for t_9be6b361: parked until 19:20 with a clock, while its condition (a Watch
build row) was already true at 10:58; nothing looked until David asked. [daveey]
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as kwc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    yield home
    kwc._shutdown_for_tests()


def _running(conn) -> str:
    tid = kb.create_task(conn, title="t", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def _events(conn, tid, kind):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


def _worker_env(monkeypatch, conn, tid):
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb.get_task(conn, tid).current_run_id))


def _flag(tmp_path: Path) -> Path:
    return tmp_path / "cw-flag"


def _probe(flag: Path) -> str:
    return f"test -f {flag} && cat {flag} || exit 1"


def _assert_condition_wake(conn, tid, flag: Path) -> None:
    park = _events(conn, tid, "scheduled")[-1].payload
    assert park["wake_check"] == _probe(flag) and park["check_every"] == 300
    wake_at = park["wake_at"]
    now = int(time.time())
    # Probe exits 1: still scheduled; the next probe is due one interval later.
    kwc.run_due_wake_checks(conn, now=now, wait=True)
    assert kb.get_task(conn, tid).status == "scheduled"
    state = kwc.get_state(conn, tid)
    assert state["last_rc"] == 1 and state["next_at"] >= now + 300 and state["fails"] == 0
    # Not due yet: nothing runs.
    assert kwc.run_due_wake_checks(conn, now=now + 10, wait=True)["submitted"] == []
    flag.write_text("build 559 seen\n")
    kwc.run_due_wake_checks(conn, now=now + 301, wait=True)
    task = kb.get_task(conn, tid)
    assert task.status == "ready"
    met = _events(conn, tid, "condition_met")
    assert met and "build 559 seen" in met[-1].payload["output"]
    assert met[-1].created_at < wake_at
    # The next worker sees why it woke.
    assert "build 559 seen" in kb.build_worker_context(conn, tid)


def test_kanban_schedule_wake_check_wakes_when_probe_passes(kanban_home, tmp_path, monkeypatch) -> None:
    from tools import kanban_tools as kt

    flag = _flag(tmp_path)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _worker_env(monkeypatch, conn, tid)
        out = json.loads(kt._handle_schedule({
            "reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting for build 559",
            "wake_check": _probe(flag), "check_every": 300}))
        assert out.get("status") == "scheduled", out
        _assert_condition_wake(conn, tid, flag)


def test_kanban_block_time_wake_check_wakes_when_probe_passes(kanban_home, tmp_path, monkeypatch) -> None:
    from tools import kanban_tools as kt

    flag = _flag(tmp_path)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET goal_mode = 1 WHERE id = ?", (tid,))
        _worker_env(monkeypatch, conn, tid)
        out = json.loads(kt._handle_block({
            "reason": "waiting for build 559", "kind": "time",
            "wake_check": _probe(flag), "check_every": 300}))
        assert out.get("status") == "scheduled", out
        _assert_condition_wake(conn, tid, flag)


@pytest.mark.parametrize("cmd", ["exit 2", "sleep 5"])
def test_broken_probe_wakes_card_after_three_failures(kanban_home, monkeypatch, cmd) -> None:
    monkeypatch.setattr(kwc, "PROBE_TIMEOUT_SECONDS", 1)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
                                wake_check=cmd, check_every=120)
        now = int(time.time())
        for i in range(2):
            kwc.run_due_wake_checks(conn, now=now + i * 200, wait=True)
            assert kb.get_task(conn, tid).status == "scheduled"
            assert kwc.get_state(conn, tid)["fails"] == i + 1
        kwc.run_due_wake_checks(conn, now=now + 400, wait=True)
        assert kb.get_task(conn, tid).status == "ready"
        broken = _events(conn, tid, "wake_check_broken")
        assert broken and broken[-1].payload["rc"] == (2 if cmd == "exit 2" else "timeout")
        assert not _events(conn, tid, "condition_met")


def test_long_park_without_wake_check_is_refused(kanban_home, monkeypatch) -> None:
    from tools import kanban_tools as kt

    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _worker_env(monkeypatch, conn, tid)
        far = "SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on the nightly tag"
        out = json.loads(kt._handle_schedule({"reason": far}))
        assert "error" in out and "wake_check" in out["error"] and "gh api" in out["error"]
        assert kb.get_task(conn, tid).status == "running"
        # kind=time with no marker defaults to a 24 h wake: also refused.
        out = json.loads(kt._handle_block({"reason": "soak", "kind": "time"}))
        assert "error" in out and kb.get_task(conn, tid).status == "running"
        # A pure clock wait says so.
        out = json.loads(kt._handle_block({"reason": far, "kind": "time", "clock_only": True}))
        assert out.get("status") == "scheduled", out
        assert _events(conn, tid, "scheduled")[-1].payload["clock_only"] is True


def test_kanban_continue_long_wait_needs_wake_check(kanban_home, tmp_path, monkeypatch) -> None:
    from tools import kanban_tools as kt

    flag = _flag(tmp_path)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _worker_env(monkeypatch, conn, tid)
        out = json.loads(kt._handle_continue({"checkpoint": "waiting on CI", "resume_after_seconds": 43200}))
        assert "error" in out and kb.get_task(conn, tid).status == "running"
        out = json.loads(kt._handle_continue({"checkpoint": "waiting on CI", "resume_after_seconds": 43200,
                                              "wake_check": _probe(flag), "check_every": 300}))
        assert out.get("status") == "scheduled", out
        assert _events(conn, tid, "continued")[-1].payload["wake_check"] == _probe(flag)
        flag.write_text("ci green\n")
        kwc.run_due_wake_checks(conn, wait=True)
        assert kb.get_task(conn, tid).status == "ready"
        assert "ci green" in _events(conn, tid, "condition_met")[-1].payload["output"]


def test_short_park_without_wake_check_is_accepted(kanban_home, monkeypatch) -> None:
    from datetime import UTC, datetime

    from tools import kanban_tools as kt

    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _worker_env(monkeypatch, conn, tid)
        soon = datetime.fromtimestamp(time.time() + 3600, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = json.loads(kt._handle_schedule({"reason": f"SCHEDULED_UNTIL={soon} CI"}))
        assert out.get("status") == "scheduled", out
        assert kwc.get_state(conn, tid) is None


def test_slow_probe_does_not_stall_the_dispatcher_tick(kanban_home) -> None:
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect_closing() as conn:
        tid = _running(conn)
        assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
                                wake_check="sleep 40", check_every=120)
        started = time.monotonic()
        result = kbd.DispatchResult()
        kbd._run_reclaim_phase(conn, result, stale_timeout_seconds=3600, failure_limit=2,
                               reconcile_orphans=False)
        assert time.monotonic() - started < 5
        assert kwc.in_flight() == [tid], kwc.get_state(conn, tid)
        # A second tick while it runs does not start a duplicate.
        kbd._run_reclaim_phase(conn, kbd.DispatchResult(), stale_timeout_seconds=3600,
                               failure_limit=2, reconcile_orphans=False)
        assert kwc.in_flight() == [tid]
        assert kb.get_task(conn, tid).status == "scheduled"


def test_wake_check_state_is_visible_in_show_and_context(kanban_home) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
                                wake_check="exit 1", check_every=120)
        kwc.run_due_wake_checks(conn, wait=True)
        ctx = kb.build_worker_context(conn, tid)
        assert "Wake check" in ctx and "exit 1" in ctx and "last rc 1" in ctx


def test_cli_wake_check_flags_round_trip(kanban_home, capsys) -> None:
    from hermes_cli import kanban as kcli

    with kbc.connect_closing() as conn:
        a, b = _running(conn), _running(conn)
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kcli.build_parser(parser.add_subparsers(dest="command"))

    def run(*argv):
        return kcli.kanban_command(parser.parse_args(["kanban", *argv]))

    assert run("schedule", a, "SCHEDULED_UNTIL=2099-01-01T00:00:00Z", "--wake-check", "test -f /x",
               "--check-every", "600") == 0
    assert run("block", b, "SCHEDULED_UNTIL=2099-01-01T00:00:00Z", "--kind", "time",
               "--wake-check", "exit 1") == 0
    with kbc.connect_closing() as conn:
        assert kwc.get_state(conn, a)["cmd"] == "test -f /x" and kwc.get_state(conn, a)["every"] == 600
        assert kwc.get_state(conn, b)["every"] == kwc.DEFAULT_CHECK_EVERY
        c = _running(conn)
    # The guard holds on the CLI too, and --clock-only lifts it.
    assert run("schedule", c, "SCHEDULED_UNTIL=2099-01-01T00:00:00Z") != 0
    assert run("schedule", c, "SCHEDULED_UNTIL=2099-01-01T00:00:00Z", "--clock-only") == 0
    capsys.readouterr()
    assert run("show", a, "--json") == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["wake_check"]["cmd"] == "test -f /x" and shown["wake_check"]["every"] == 600


def test_check_every_is_clamped(kanban_home) -> None:
    assert kwc.normalize_every(None) == 900
    assert kwc.normalize_every(5) == 120
    assert kwc.normalize_every(10**6) == 21600


def test_diagnostics_flag_clock_parks_and_failing_probes(kanban_home) -> None:
    from hermes_cli import kanban_diagnostics as kd

    with kbc.connect_closing() as conn:
        legacy, failing, ok = _running(conn), _running(conn), _running(conn)
        # Legacy: parked 24 h out with no probe and no clock_only (old code path).
        assert kb.schedule_task(conn, legacy, reason="waiting on a tag")
        assert kb.schedule_task(conn, failing, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
                                wake_check="exit 2", check_every=120)
        assert kb.schedule_task(conn, ok, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
                                clock_only=True)
        kwc.run_due_wake_checks(conn, wait=True)
        states = kwc.load_states(conn)

        def kinds(tid, now=None):
            return {d.kind for d in kd.compute_task_diagnostics(
                kb.get_task(conn, tid), kb.list_events(conn, tid), kb.list_runs(conn, tid),
                wake_check=states.get(tid), now=now)}

        assert "clock_only_park" in kinds(legacy)
        assert "wake_check_failing" in kinds(failing)
        assert not ({"clock_only_park", "wake_check_failing"} & kinds(ok))
        # A probe that has not run long after it was due is flagged too.
        with kb.write_txn(conn):
            conn.execute("UPDATE task_wake_checks SET last_rc = 1, fails = 0 WHERE task_id = ?", (failing,))
        states = kwc.load_states(conn)
        assert "wake_check_overdue" in kinds(failing, now=states[failing]["next_at"] + 3600)
