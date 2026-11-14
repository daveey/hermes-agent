"""Same-second recovery fence and raced-check cadence for shared blocker checks."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_shared_blocker_checks as checks
from hermes_cli import kanban_shared_blockers as blockers
from tests.hermes_cli import test_kanban_shared_blockers as helpers

kanban_home = helpers.kanban_home
_blocked_behind = helpers._blocked_behind
GH = "github-rest-quota"
CFG = {"kanban": {"shared_blocker_checks": {GH: "unused"}}}


@pytest.fixture
def clock(monkeypatch):
    now = [1800000000]
    monkeypatch.setattr(blockers.time, "time", lambda: now[0])
    monkeypatch.setattr(checks.time, "time", lambda: now[0])
    return now


def _check_ok(conn, monkeypatch):
    monkeypatch.setattr(checks, "_run_check", lambda c: (0, ""))
    assert checks.run_due_checks(conn, cfg=CFG) == [GH]


@pytest.mark.parametrize('end_delay', [0, 5])
def test_same_second_claim_after_check_reopens(kanban_home, monkeypatch, clock, end_delay):
    with kbc.connect_closing() as conn:
        _blocked_behind(conn, "quota", blocker_id=GH)
        worker = kb.create_task(conn, title="new worker", assignee="default")
        clock[0] += 30
        _check_ok(conn, monkeypatch)
        assert kb.claim_task(conn, worker)  # same second as checked_at, after the receipt
        clock[0] += end_delay
        assert kb.block_task(conn, worker, reason="new outage", kind="capability", blocker_id=GH)
        assert blockers.blocker_states(conn)[GH].is_open


def test_next_second_claim_after_check_reopens(kanban_home, monkeypatch, clock):
    with kbc.connect_closing() as conn:
        _blocked_behind(conn, "quota", blocker_id=GH)
        worker = kb.create_task(conn, title="new worker", assignee="default")
        clock[0] += 30
        _check_ok(conn, monkeypatch)
        clock[0] += 1
        assert kb.claim_task(conn, worker)
        clock[0] += 5
        assert kb.block_task(conn, worker, reason="new outage", kind="capability", blocker_id=GH)
        assert blockers.blocker_states(conn)[GH].is_open


def test_same_second_admitted_during_command_stays_fenced(kanban_home, monkeypatch, clock):
    with kbc.connect_closing() as conn:
        _blocked_behind(conn, "quota", blocker_id=GH)
        worker = kb.create_task(conn, title="in flight", assignee="default")
        clock[0] += 30

        def run(_command):
            assert kb.claim_task(conn, worker)  # admitted while the command runs
            return 0, ""

        monkeypatch.setattr(checks, "_run_check", run)
        assert checks.run_due_checks(conn, cfg=CFG) == [GH]
        clock[0] += 5
        assert kb.block_task(conn, worker, reason="stale quota", kind="capability", blocker_id=GH)
        assert not blockers.blocker_states(conn)[GH].is_open


def test_receipt_records_active_run_snapshot(kanban_home, monkeypatch, clock):
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "quota", blocker_id=GH)
        clock[0] += 30
        _check_ok(conn, monkeypatch)
        receipt = checks._latest_results(conn)[GH]
        assert receipt["active_runs"] == []
        assert tid


def test_older_receipt_uses_strict_timestamp_fallback(kanban_home, monkeypatch, clock):
    with kbc.connect_closing() as conn:
        _blocked_behind(conn, "quota", blocker_id=GH)
        worker = kb.create_task(conn, title="new worker", assignee="default")
        clock[0] += 30
        _check_ok(conn, monkeypatch)
        row = conn.execute("SELECT id, task_id, payload FROM task_events WHERE kind=?",
                           (checks.CHECK_EVENT,)).fetchone()
        payload = kb._json_dict(row["payload"])
        payload.pop("active_runs")
        import json
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
        assert kb.claim_task(conn, worker)  # same second, no snapshot on receipt
        clock[0] += 5
        assert kb.block_task(conn, worker, reason="new outage", kind="capability", blocker_id=GH)
        assert blockers.blocker_states(conn)[GH].is_open


def test_raced_check_keeps_cadence_without_clearing(kanban_home, monkeypatch, clock):
    cfg = {"kanban": {"shared_blocker_checks": {"test-wall": "x"}}}
    calls = []
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "a", blocker_id="test-wall")

        def run(_command):
            calls.append(clock[0])
            _blocked_behind(conn, f"again{len(calls)}", blocker_id="test-wall")
            return 0, ""

        monkeypatch.setattr(checks, "_run_check", run)
        due = []
        for _ in range(3):
            checks.run_due_checks(conn, cfg=cfg, due_out=due, now=clock[0])
            clock[0] += 5
        assert len(calls) == 1
        assert due and min(due) == 1800000000 + checks.CHECK_EVERY_SECONDS
        events = [e for e in kb.list_events(conn, tid) if e.kind == checks.CHECK_EVENT]
        assert not events  # discarded result is never success evidence
        assert blockers.blocker_states(conn)["test-wall"].is_open
        attempts = [e for e in kb.list_events(conn, tid) if e.kind == checks.ATTEMPT_EVENT]
        assert len(attempts) == 1 and attempts[0].payload["rc"] == "discarded"
        # Cadence expires: the next window runs again.
        clock[0] = 1800000000 + checks.CHECK_EVERY_SECONDS
        checks.run_due_checks(conn, cfg=cfg, now=clock[0])
        assert len(calls) == 2
