"""Finite CLI readers must finish count agreement before printing/exiting. [daveey]"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("audit", [False, True])
@pytest.mark.parametrize("case", ["empty", "open", "error", "timeout"])
def test_cli_waits_for_count_publication_before_returning(tmp_path, audit, case):
    """Hold a real agreement flight past the interactive bound in a fresh interpreter.

    The parent owns the release pipe: no callback or test thread keeps the CLI alive.
    Both the printed result and the separately reopened store must be final.
    """
    script = r'''
import json, sys
from pathlib import Path
from types import SimpleNamespace
import hermes_state_outcome as so
from hermes_state import SessionDB
from hermes_cli import kanban_counts_agreement as agreement
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import sessions_cmd_outcome as cli
home = Path(sys.argv[1])
so._reset_hooks_for_tests()
ksa._board_db_paths = lambda: []
ksa._hub_db_path = lambda: None
ksa.session_outcome_tasks = lambda *a, **k: []
cli.ensure_outcome_hooks = lambda: None
cli._stripe_report = lambda *a: None
cli._surface_reports = lambda *a: {}
cli.sidebar_rows = lambda *a: [{"id": "chat", "title": "fixture"}]
so._cron_items = lambda *a: []
so._watch_items = lambda *a: []
def cold():
    raise agreement.CountsPending("cold process")
agreement.fresh_open_counts = cold
if sys.argv[3] == "timeout":
    agreement.FOREGROUND_WAIT_S = 0.05
def held():
    print("counts-started", file=sys.stderr, flush=True)
    assert sys.stdin.readline().strip() == "release"
    if sys.argv[3] == "error":
        raise OSError("count source unavailable")
    return {"chat": 1} if sys.argv[3] == "open" else {}
ksa.active_task_counts = held
ksa.blocked_task_counts = lambda: {}
with SessionDB(db_path=home / "state.db") as db:
    db.create_session("chat", source="desktop")
    db.append_message("chat", role="user", content="please fix the flaky test")
    db.append_message("chat", role="assistant", content="Fixed the flaky test by waiting on the event instead of sleeping; all 12 tests pass now.")
    if sys.argv[2] == "audit":
        print(json.dumps(cli.audit_rows(db, refresh=True)))
    else:
        cli.cmd_outcome(db, SimpleNamespace(session_id="chat", refresh=True, json=True))
'''
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": str(Path.cwd())}
    proc = subprocess.Popen([sys.executable, "-c", script, str(tmp_path), "audit" if audit else "single", case],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env)
    try:
        assert proc.stderr is not None
        assert proc.stderr.readline().strip() == "counts-started"
        # The old CLI exits after WAIT_S with unknown, killing its passive callback.
        if case == "timeout":
            proc.wait(timeout=10)
            stdout, stderr = proc.communicate(timeout=10)
        else:
            with pytest.raises(subprocess.TimeoutExpired):
                proc.wait(timeout=1.5)
            stdout, stderr = proc.communicate("release\n", timeout=30)
        assert proc.returncode == 0, stderr
        result = json.loads(stdout)
        expected = "done" if case == "empty" else "unresolved"
        assert (result[0] if audit else result["outcome"])["kind"] == expected, (result, stderr)
        from hermes_state import SessionDB
        from hermes_state_outcome import get_outcome
        with SessionDB(db_path=tmp_path / "state.db", read_only=True) as db:
            stored = get_outcome("chat", db=db)
        assert stored is not None
        assert stored["kind"] == expected
        assert stored["status"]["coverage"]["state"] == ("complete" if case == "empty" else "unknown")
    finally:
        if proc.poll() is None:
            proc.communicate("release\n", timeout=30)


def test_foreground_budget_is_shared_and_never_registers_a_late_callback(monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import kanban_counts_agreement as agreement
    from hermes_cli import kanban_session_activity as ksa

    def cold():
        raise agreement.CountsPending("no fresh snapshot")

    waits = []
    flight = agreement._Flight()
    flight.done = SimpleNamespace(wait=waits.append, is_set=lambda: False)
    monkeypatch.setattr(agreement, "fresh_open_counts", cold)
    monkeypatch.setattr(ksa, "_origin_scope", lambda: ("fixture",))
    monkeypatch.setattr(agreement, "_flights", {("fixture",): flight})
    clock = iter([100.0, 110.0, 125.0, 135.0, 140.0])
    monkeypatch.setattr(agreement, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with agreement.foreground_counts():
        for _ in range(3):
            with pytest.raises(agreement.CountsPending):
                agreement.agreed_counts(0.0, on_published=lambda: pytest.fail("late refresh"))
    assert waits == [20.0, 5.0, 0.0]
    assert not flight.callbacks
    assert agreement._foreground_deadline.get() is None
    # A foreground exception cannot leave a long wait/disabled callback in the live worker.
    with pytest.raises(ValueError), agreement.foreground_counts():
        raise ValueError("consumer failed")
    assert agreement._foreground_deadline.get() is None


@pytest.mark.parametrize("failing_row", [False, True])
def test_audit_shares_one_count_wait_deadline_across_rows(monkeypatch, failing_row):
    from types import SimpleNamespace
    import hermes_state_outcome as so
    from hermes_cli import kanban_counts_agreement as agreement
    from hermes_cli import kanban_session_activity as ksa
    from hermes_cli import sessions_cmd_outcome as cli

    clock = [100.0]
    waits, deadlines = [], []

    def cold():
        raise agreement.CountsPending("held source")

    def wait(seconds):
        waits.append(seconds)
        clock[0] += seconds

    flight = agreement._Flight()
    flight.done = SimpleNamespace(wait=wait, is_set=lambda: False)
    monkeypatch.setattr(agreement, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(agreement, "fresh_open_counts", cold)
    monkeypatch.setattr(agreement, "_flights", {("fixture",): flight})
    monkeypatch.setattr(ksa, "_origin_scope", lambda: ("fixture",))
    monkeypatch.setattr(cli, "ensure_outcome_hooks", lambda: None)
    monkeypatch.setattr(cli, "sidebar_rows", lambda *a: [{"id": str(i)} for i in range(3)])

    def refresh(sid, *a, **kw):
        deadlines.append(agreement._foreground_deadline.get())
        with pytest.raises(agreement.CountsPending):
            agreement.agreed_counts(on_published=lambda: pytest.fail("late refresh"))
        if failing_row and sid == "1":
            raise ValueError("row failed")
        return {"kind": "unresolved", "reason": "card_read_failed"}

    monkeypatch.setattr(so, "refresh_outcome", refresh)
    if failing_row:
        with pytest.raises(ValueError, match="row failed"):
            cli.audit_rows(None, refresh=True)
    else:
        rows = cli.audit_rows(None, refresh=True)
        assert [row["reason"] for row in rows] == ["card_read_failed"] * 3
    count = 2 if failing_row else 3
    assert deadlines == [130.0] * count
    assert waits == [30.0] + [0.0] * (count - 1)
    assert not flight.callbacks
    assert agreement._foreground_deadline.get() is None
