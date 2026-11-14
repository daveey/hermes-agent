"""Updater TERM diagnostics must not consume orderly review continuations."""
import json
import sqlite3

import pytest

from hermes_cli.kanban_core_update_requeue import CORE_UPDATE_REQUEUE_REASON, is_core_update_requeue
from hermes_cli.kanban_move_parent_evidence import owner_faulted_since


@pytest.fixture
def receipt():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE task_runs (id INTEGER, task_id TEXT, outcome TEXT, ended_at INTEGER);
        CREATE TABLE task_events (task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
        INSERT INTO task_runs VALUES (7, 'owner', 'reclaimed', 100);
    """)
    reclaim = dict(manual=True, reason=CORE_UPDATE_REQUEUE_REASON, prev_pid=42,
                   prev_lock="worker:99", host_local=True, termination_attempted=True,
                   terminated=True, sigkill=False)
    crash = dict(pid=42, claimer="worker:99", exit_kind="signaled", exit_code=15)
    yield conn, reclaim, crash
    conn.close()


def populate(receipt, *, crash_time=100, crash_kind="crashed", crash_run=7):
    conn, reclaim, crash = receipt
    conn.executemany("INSERT INTO task_events VALUES ('owner', ?, ?, ?, ?)", [
        (7, "reclaimed", json.dumps(reclaim), 100),
        (crash_run, crash_kind, json.dumps(crash), crash_time),
    ])
    return conn.execute("SELECT * FROM task_runs").fetchone()


def test_matching_updater_term_is_not_a_foreign_review_fault(receipt):
    run = populate(receipt)
    conn = receipt[0]
    assert is_core_update_requeue(conn, run)
    assert not owner_faulted_since(conn, "owner", 90)
    # A genuine successor fault still consumes the continuation.
    conn.execute("INSERT INTO task_events VALUES ('owner', 8, 'crashed', '{}', 101)")
    assert owner_faulted_since(conn, "owner", 90)


@pytest.mark.parametrize("side,key,value", [
    ("reclaim", "manual", False), ("reclaim", "reason", "other"),
    ("reclaim", "prev_pid", 0), ("reclaim", "prev_pid", True),
    ("reclaim", "prev_pid", "42"), ("reclaim", "prev_lock", ""),
    ("reclaim", "prev_lock", 99), ("reclaim", "host_local", False),
    ("reclaim", "termination_attempted", False), ("reclaim", "terminated", False),
    ("reclaim", "sigkill", True), ("reclaim", "sigkill", None),
    ("crash", "pid", 43), ("crash", "pid", "42"),
    ("crash", "claimer", "other:99"), ("crash", "exit_kind", "exited"),
    ("crash", "exit_code", 9), ("crash", "exit_code", "15"),
    ("crash", "exit_code", 15.0), ("crash", "worker_lost", {}),
    ("crash", "attempt_note", "managed worker lost: EOFError"),
    ("reclaim", "prev_lock", "   "), ("crash", "pid", 42.0),
])
def test_mismatched_or_incomplete_receipt_remains_a_fault(receipt, side, key, value):
    receipt[1 if side == "reclaim" else 2][key] = value
    run = populate(receipt)
    assert not is_core_update_requeue(receipt[0], run)
    assert owner_faulted_since(receipt[0], "owner", 90)


@pytest.mark.parametrize("kind", ["timed_out", "spawn_failed", "gave_up"])
def test_other_failure_kinds_are_never_exempt(receipt, kind):
    run = populate(receipt, crash_kind=kind)
    assert not is_core_update_requeue(receipt[0], run)


@pytest.mark.parametrize("stamp", [99, 101, None])
def test_unmatched_time_is_not_termination_proof(receipt, stamp):
    run = populate(receipt, crash_time=stamp)
    assert not is_core_update_requeue(receipt[0], run)


def test_no_crash_requeue_is_not_a_foreign_review_fault(receipt):
    run = populate(receipt)
    receipt[0].execute("DELETE FROM task_events WHERE kind = 'crashed'")
    assert is_core_update_requeue(receipt[0], run)
    assert not owner_faulted_since(receipt[0], "owner", 90)


def test_failure_on_another_run_is_not_erased(receipt):
    run = populate(receipt, crash_run=8)
    assert is_core_update_requeue(receipt[0], run)
    assert owner_faulted_since(receipt[0], "owner", 90)


@pytest.mark.parametrize("side,key", [
    (1, "prev_pid"), (1, "prev_lock"), (1, "host_local"), (1, "termination_attempted"),
    (1, "terminated"), (1, "sigkill"), (2, "pid"), (2, "claimer"),
    (2, "exit_kind"), (2, "exit_code"),
])
def test_missing_termination_fields_fail_closed(receipt, side, key):
    del receipt[side][key]
    run = populate(receipt)
    assert not is_core_update_requeue(receipt[0], run)


@pytest.mark.parametrize("case", ["reclaim_time", "run_end", "outcome", "no_reclaim", "extra_failure"])
def test_conflicting_evidence_fails_closed(receipt, case):
    populate(receipt)
    conn = receipt[0]
    statements = {
        "reclaim_time": "UPDATE task_events SET created_at = 99 WHERE kind = 'reclaimed'",
        "run_end": "UPDATE task_runs SET ended_at = 99",
        "outcome": "UPDATE task_runs SET outcome = 'crashed'",
        "no_reclaim": "DELETE FROM task_events WHERE kind = 'reclaimed'",
        "extra_failure": "INSERT INTO task_events VALUES ('owner', 7, 'crashed', '{}', 100)",
    }
    conn.execute(statements[case])
    assert not is_core_update_requeue(conn, conn.execute("SELECT * FROM task_runs").fetchone())


def test_fault_before_checkpoint_does_not_invalidate_it(receipt):
    populate(receipt, crash_time=99)
    assert not owner_faulted_since(receipt[0], "owner", 101)
