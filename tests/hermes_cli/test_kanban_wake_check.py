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


# A worker parking its own card on an opaque probe names who makes it pass (kanban_awaiting opaque_probe_wait).
BUILD = {"kind": "external", "ref": "release build 559", "why": "the build job writes the flag when it lands"}


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


@pytest.mark.parametrize('spent', [False, True])
def test_last_budgeted_run_is_reserved_for_own_condition(kanban_home, tmp_path, spent):
    """A generic wake cannot spend the final run while the terminal log probe is false."""
    from hermes_cli import kanban_spend_budget as budget
    from hermes_cli.kanban_continuation import _wake_parks
    flag = tmp_path / 'terminal-exit'
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        if spent:
            with kb.write_txn(conn):
                conn.execute('UPDATE task_runs SET started_at=? WHERE task_id=?', (int(time.time()) - 120, tid))
        budget.set_override(conn, tid, runs=2 if spent else 1, by='owner', reason='one authorized settlement run')
        assert kb.schedule_task(conn, tid, reason='SCHEDULED_UNTIL=2099-01-01T00:00:00Z',
                                wake_check=_probe(flag), check_every=120)
        count = conn.execute('SELECT count(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0]
        assert _wake_parks(conn, 4070908801)[0] == []
        assert kb.get_task(conn, tid).status == 'scheduled'
        assert kwc.get_state(conn, tid) is not None
        assert len(_events(conn, tid, 'terminal_run_reserved')) == 1
        flag.write_text('bb land: exit 0\n')
        kwc.run_due_wake_checks(conn, now=int(time.time()) + 121, wait=True)
        assert kb.get_task(conn, tid).status == 'ready'
        assert _events(conn, tid, 'condition_met')[-1].payload['output'] == 'bb land: exit 0'
        assert conn.execute('SELECT count(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0] == count
        assert budget.hold(conn, tid) is None  # original limit remains, no extra grant


@pytest.mark.parametrize('kind', ['wake_check_stale', 'wake_check_broken', 'wake_probe_lost', 'condition_met'])
def test_terminal_reservation_never_swallows_probe_repair_or_terminal(kanban_home, tmp_path, kind):
    from hermes_cli import kanban_spend_budget as budget
    from hermes_cli.kanban_terminal_run import reserve
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        budget.set_override(conn, tid, runs=1, by='owner', reason='bounded')
        assert kb.schedule_task(conn, tid, reason='SCHEDULED_UNTIL=2099-01-01T00:00:00Z',
                                wake_check=_probe(tmp_path / 'terminal'), check_every=120)
        with kb.write_txn(conn):
            assert not reserve(conn, tid, kind)
        assert not _events(conn, tid, 'terminal_run_reserved')


def test_kanban_schedule_wake_check_wakes_when_probe_passes(kanban_home, tmp_path, monkeypatch) -> None:
    from tools import kanban_tools as kt

    flag = _flag(tmp_path)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _worker_env(monkeypatch, conn, tid)
        out = json.loads(kt._handle_schedule({
            "reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting for build 559",
            "wake_check": _probe(flag), "check_every": 300, "awaiting": BUILD}))
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
            "wake_check": _probe(flag), "check_every": 300, "awaiting": BUILD}))
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
        out = json.loads(kt._handle_block({"reason": far, "kind": "time", "clock_only": True, "awaiting": {"kind": "clock", "why": "fixture window"}}))
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
                                              "wake_check": _probe(flag), "check_every": 300, "awaiting": BUILD}))
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
        out = json.loads(kt._handle_schedule({"reason": f"SCHEDULED_UNTIL={soon} CI",
                                             "awaiting": {"kind": "clock", "why": "fixture window"}}))
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
    assert run("schedule", c, "SCHEDULED_UNTIL=2099-01-01T00:00:00Z", "--clock-only", "--awaiting", "clock:", "--awaiting-why", "fixture window") == 0
    capsys.readouterr()
    assert run("show", a, "--json") == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["wake_check"]["cmd"] == "test -f /x" and shown["wake_check"]["every"] == 600


@pytest.mark.parametrize("rc", [0, 2])
def test_inflight_same_command_repark_discards_old_result(kanban_home, monkeypatch, rc):
    import threading

    started, release = threading.Event(), threading.Event()

    def probe(*args):
        started.set()
        assert release.wait(10)
        return rc, "old observation"

    monkeypatch.setattr(kwc, "_run_probe", probe)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        assert kb.schedule_task(conn, tid, wake_check="exit 0")
        kwc.run_due_wake_checks(conn)
        assert started.wait(10)
        try:
            assert kb.unblock_task(conn, tid)
            assert kb.schedule_task(conn, tid, wake_check="exit 0")
            before = kwc.get_state(conn, tid)
        finally:
            release.set()
            kwc._shutdown_for_tests()
        assert kb.get_task(conn, tid).status == "scheduled"
        assert kwc.get_state(conn, tid) == before
        assert not _events(conn, tid, "condition_met")
        # The current generation still applies both success and failure normally.
        kwc.run_due_wake_checks(conn, wait=True)
        if rc == 0:
            assert kb.get_task(conn, tid).status == "ready"
        else:
            assert kwc.get_state(conn, tid)["fails"] == 1


@pytest.mark.parametrize("op", ["schedule", "block", "continue"])
def test_shared_transition_rejects_long_unprobed_park_atomically(kanban_home, op):
    from hermes_cli.kanban_continuation import continue_task

    with kbc.connect_closing() as conn:
        tid = _running(conn)
        events = kb.list_events(conn, tid)
        runs = kb.list_runs(conn, tid)
        if op == "continue":
            result = continue_task(conn, tid, checkpoint="wait", resume_after_seconds=86400)
            assert not result["ok"] and "wake_check" in result["error"]
        else:
            with pytest.raises(ValueError, match="wake_check"):
                if op == "schedule":
                    kb.schedule_task(conn, tid)
                else:
                    kb.block_task(conn, tid, kind="time", reason="wait")
        assert kb.get_task(conn, tid).status == "running"
        assert kb.list_events(conn, tid) == events
        assert kb.list_runs(conn, tid) == runs


def test_legacy_probe_schema_migrates_without_reparking(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        assert kb.schedule_task(conn, tid, wake_check="exit 0")
        events = kb.list_events(conn, tid)
        with kb.write_txn(conn):
            conn.execute("ALTER TABLE task_wake_checks DROP COLUMN generation")
    kb.init_db()
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "scheduled"
        assert kb.list_events(conn, tid) == events
        assert kwc.get_state(conn, tid)["generation"] is None
        kwc.run_due_wake_checks(conn, wait=True)
        assert kb.get_task(conn, tid).status == "ready"


@pytest.mark.parametrize("op", ["schedule", "block", "continue"])
@pytest.mark.parametrize("options", [{"clock_only": True, "awaiting": {"kind": "clock", "why": "fixture window"}}, {"wake_check": "exit 1"}])
def test_shared_transition_allows_explicit_long_parks(kanban_home, op, options):
    from hermes_cli.kanban_continuation import continue_task

    with kbc.connect_closing() as conn:
        tid = _running(conn)
        if op == "continue":
            assert continue_task(conn, tid, checkpoint="wait", resume_after_seconds=86400, **options)["ok"]
        elif op == "block":
            assert kb.block_task(conn, tid, reason="wait", kind="time", **options)
        else:
            assert kb.schedule_task(conn, tid, **options)
        assert kb.get_task(conn, tid).status == "scheduled"


def test_check_every_is_clamped(kanban_home) -> None:
    assert kwc.normalize_every(None) == 900
    assert kwc.normalize_every(5) == 120
    assert kwc.normalize_every(10**6) == 21600


def test_diagnostics_flag_clock_parks_and_failing_probes(kanban_home) -> None:
    from hermes_cli import kanban_diagnostics as kd

    with kbc.connect_closing() as conn:
        legacy, failing, ok = _running(conn), _running(conn), _running(conn)
        # Legacy: parked 24 h out with no probe and no clock_only (old code path).
        # Deliberately emulate an old persisted park; new writes must pass the guard.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (legacy,))
            kb._append_event(conn, legacy, "scheduled", {
                "reason": "waiting on a tag", "wake_at": int(time.time()) + 86400})
        assert kb.schedule_task(conn, failing, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
                                wake_check="exit 2", check_every=120)
        assert kb.schedule_task(conn, ok, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
                                clock_only=True, awaiting={"kind": "clock", "why": "fixture window"})
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


# --- silent forever-waits (t_1132a78a) -----------------------------------------------------

def test_dry_run_rejects_probes_that_can_never_succeed():
    # exit 0 = the condition already holds: the park would wake at once (wake_check_already_true)
    assert kwc.dry_run_error("exit 0").startswith(kwc.WAKE_CHECK_ALREADY_TRUE)
    assert kwc.dry_run_error("exit 1") is None
    assert "exited 2" in kwc.dry_run_error("exit 2")
    assert "exited 127" in kwc.dry_run_error("no_such_command_xyz")
    # exit 1 with a traceback / shell syntax error / sqlite error is still broken
    assert kwc.dry_run_error("echo 'OperationalError: incomplete input' >&2; exit 1")
    assert kwc.dry_run_error("echo \"bash: -c: line 1: syntax error near unexpected token '('\"; exit 1")
    assert kwc.dry_run_error("echo 'error: database is locked'; exit 1")
    assert kwc.dry_run_error("echo 'task t_x running'; exit 1") is None


def test_dry_run_can_be_disabled(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    assert kwc.dry_run_error("exit 2") is None


def test_exit1_with_error_output_counts_as_failing_then_wakes_broken(kanban_home):
    cmd = "echo 'Traceback (most recent call last):' ; exit 1"
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
                                wake_check=cmd, check_every=120)
        for i in range(kwc.BROKEN_AFTER):
            state = kwc.get_state(conn, tid)
            gen = state["generation"]
            kwc.apply_result(conn, tid, cmd, 1, "Traceback (most recent call last):", generation=gen, now=1000 + i)
            if i < kwc.BROKEN_AFTER - 1:
                assert kwc.get_state(conn, tid)["fails"] == i + 1
        assert _events(conn, tid, kwc.BROKEN_EVENT)


def test_plain_exit1_is_still_not_yet(kanban_home):
    cmd = "echo 'running'; exit 1"
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
                                wake_check=cmd, check_every=120)
        gen = kwc.get_state(conn, tid)["generation"]
        kwc.apply_result(conn, tid, cmd, 1, "t_x running", generation=gen, now=1000)
        assert kwc.get_state(conn, tid)["fails"] == 0


def test_cli_park_rejects_broken_probe():
    from hermes_cli.kanban import _cli_wake_check

    ns = argparse.Namespace(wake_check="exit 2", check_every=None, clock_only=False)
    with pytest.raises(ValueError, match="never succeed|could never"):
        _cli_wake_check(ns)


def test_wait_check_local(kanban_home):
    from hermes_cli import kanban_wait_check as w

    with kbc.connect_closing() as conn:
        tid = _running(conn)
    assert w.check_local("task", tid, until=("done",))[0] == 1
    assert w.check_local("task", tid, until=("running",))[0] == 0
    rc, msg = w.check_local("task", "t_missing")
    assert rc == 2 and msg.startswith("error:")
    assert w.check_local("board-empty", "default")[0] == 1


def test_wait_check_remote_is_argv_safe(monkeypatch):
    from hermes_cli import kanban_wait_check as w

    seen = {}

    class P:
        returncode, stdout, stderr = 1, "t_x running", ""

    def fake(argv, **kw):
        seen["argv"] = argv
        return P()

    monkeypatch.setattr(w.subprocess, "run", fake)
    assert w.check_remote("hub", "task", "t_x; rm -rf /", until=("done",))[0] == 1
    assert seen["argv"][-2] == "ubuntu@100.104.201.88"
    assert "'t_x; rm -rf /'" in seen["argv"][-1]
    P.returncode, P.stdout = 255, ""
    assert w.check_remote("hub", "task", "t_x")[0] == 2


def _scheduled_probe(conn, monkeypatch, cmd):
    from tools import kanban_tools as kt

    tid = _running(conn)
    _worker_env(monkeypatch, conn, tid)
    out = json.loads(kt._handle_schedule({
        "reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting", "wake_check": cmd,
        "check_every": 300, "awaiting": BUILD}))
    assert out.get("status") == "scheduled", out
    return tid


def test_stray_exit0_does_not_wake_when_confirmation_run_fails(kanban_home, tmp_path, monkeypatch) -> None:
    """A probe that exits 0 once then 1 (a stolen/garbled status) must not wake the card."""
    counter = tmp_path / "runs"
    cmd = f"echo x >> {counter}; test $(wc -l < {counter}) -le 1"  # run 1 -> 0, run 2+ -> 1
    with kbc.connect_closing() as conn:
        monkeypatch.setattr(kwc, "dry_run_error", lambda *a, **k: None)
        tid = _scheduled_probe(conn, monkeypatch, cmd)
        counter.unlink(missing_ok=True)
        kwc.run_due_wake_checks(conn, now=int(time.time()), wait=True)
        assert kb.get_task(conn, tid).status == "scheduled"
        assert not _events(conn, tid, "condition_met")
        assert len(counter.read_text().splitlines()) == 2  # probed, then confirmed


def test_condition_met_payload_carries_diagnostics(kanban_home, tmp_path, monkeypatch) -> None:
    flag = _flag(tmp_path)
    flag.write_text("ok\n")
    with kbc.connect_closing() as conn:
        monkeypatch.setattr(kwc, "dry_run_error", lambda *a, **k: None)
        tid = _scheduled_probe(conn, monkeypatch, _probe(flag))
        kwc.run_due_wake_checks(conn, now=int(time.time()), wait=True)
        ev = _events(conn, tid, "condition_met")
        assert len(ev) == 1
        d = ev[0].payload["diagnostics"]
        assert d["pid"] and d["host"] and d["cwd"] and "home" in d
        assert isinstance(d["path_head"], list) and d["first_rc"] == 0 and d["confirm_rc"] == 0


def test_reaper_does_not_steal_wake_probe_status(kanban_home, monkeypatch) -> None:
    """reap_worker_zombies used waitpid(-1): it reaped a probe child and Popen then read exit 1 as 0."""
    import subprocess
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(kbd, "_live_worker_procs", {})
    proc = subprocess.Popen(["bash", "-c", "exit 1"], start_new_session=True)
    time.sleep(0.5)
    assert proc.pid not in kbd.reap_worker_zombies()
    assert proc.wait() == 1
