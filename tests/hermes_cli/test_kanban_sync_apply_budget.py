"""Bounded feed transactions retain ordered durable cursor prefixes."""
from __future__ import annotations

import sqlite3
import threading
import time
from itertools import pairwise

import pytest

from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_apply_budget as budget
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.test_kanban_sync_apply import _ch, _task_fields


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    yield from support.synced_conn(home)


def test_large_page_yields_to_dispatcher(conn, monkeypatch):
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    if not path:
        pytest.fail("concurrency proof needs a file-backed board")
    page = [_ch(i, "tasks", f"t_budget{i:016x}", _task_fields()) for i in range(1, 193)]
    started, acquired = threading.Event(), threading.Event()
    errors = []
    waits = []

    def dispatcher():
        try:
            with sqlite3.connect(path, timeout=2) as other:
                started.wait(2)
                before = time.monotonic()
                other.execute("BEGIN IMMEDIATE")
                waits.append(time.monotonic() - before)
                other.execute("CREATE TABLE dispatcher_probe (value INTEGER)")
                other.execute("INSERT INTO dispatcher_probe VALUES (1)")
                other.commit()
                acquired.set()
        except (sqlite3.Error, RuntimeError) as exc:
            errors.append(exc)

    original = sync._apply_one
    observed = []

    def apply_one(db, change, own, result):
        started.set()
        if change.seq > budget.MAX_ROWS:
            observed.append(acquired.is_set())
        original(db, change, own, result)

    monkeypatch.setattr(sync, "_apply_one", apply_one)
    thread = threading.Thread(target=dispatcher)
    thread.start()
    try:
        result = budget.apply_page(conn, page, new_cursor=200, observers=False)
    finally:
        thread.join(3)
    assert not thread.is_alive() and not errors
    assert acquired.is_set() and any(observed)
    assert waits and waits[0] < budget.MAX_TRANSACTION_S + 0.5
    assert result.applied == len(page)
    assert sync.sync_state(conn)["cursor"] == 200
    assert conn.execute("SELECT count(*) FROM tasks WHERE id LIKE 't_budget%'").fetchone()[0] == len(page)


def test_crash_resumes_committed_prefix(conn, monkeypatch):
    page = [_ch(i, "tasks", f"t_resume{i:016x}", _task_fields()) for i in range(1, 90)]
    original = sync._apply_one

    def crash(db, change, own, result):
        if change.seq == budget.MAX_ROWS + 2:
            raise RuntimeError("injected crash")
        original(db, change, own, result)

    monkeypatch.setattr(sync, "_apply_one", crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        budget.apply_page(conn, page, new_cursor=100, observers=False)
    cursor = sync.sync_state(conn)["cursor"]
    assert cursor == budget.MAX_ROWS
    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM tasks WHERE id LIKE 't_resume%'").fetchone()[0] == cursor
    monkeypatch.setattr(sync, "_apply_one", original)
    budget.apply_page(conn, [ch for ch in page if ch.seq > cursor], new_cursor=100, observers=False)
    budget.apply_page(conn, page, new_cursor=100, observers=False)
    assert conn.execute("SELECT count(*) FROM tasks WHERE id LIKE 't_resume%'").fetchone()[0] == len(page)


def test_schema_lookups_outside_writer(conn):
    import traceback
    stacks = []
    statements = []
    def trace(sql):
        statements.append((conn.in_transaction, sql))
        if conn.in_transaction and "TABLE_INFO" in sql.upper() and len(stacks) < 4:
            stacks.append("".join(traceback.format_stack(limit=12)))
    conn.set_trace_callback(trace)
    try:
        page = [_ch(i, "tasks", f"t_schema{i:016x}", _task_fields()) for i in range(1, 80)]
        budget.apply_page(conn, page, new_cursor=100, observers=False)
    finally:
        conn.set_trace_callback(None)
    lookups = [(inside, sql) for inside, sql in statements if "TABLE_INFO" in sql.upper()]
    assert lookups and not any(inside for inside, _ in lookups), "\n".join(stacks)
    assert len([sql for _, sql in lookups if not sql.lstrip().startswith("--")]) <= len(sync.SYNCED_TABLES) + 1


def test_sql_deadline_rolls_back_single_row(conn, monkeypatch):
    monkeypatch.setattr(budget, "MAX_ROWS", 1)
    def expensive(db, change, own, result):
        db.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) "
                   "SELECT sum(x) FROM n").fetchone()
    monkeypatch.setattr(sync, "_apply_one", expensive)
    start = time.monotonic()
    with pytest.raises(budget.ApplyBudgetExceeded):
        budget.apply_page(conn, [_ch(1, "tasks", "t_deadline00000001", _task_fields())],
                          new_cursor=1, observers=False)
    assert time.monotonic() - start < 2
    assert not conn.in_transaction
    assert sync.sync_state(conn)["cursor"] == 0
    assert sync.sync_state(conn)["applying"] == 0
    assert conn.execute("SELECT 42").fetchone()[0] == 42


def test_slow_batch_retries_smaller_without_reordering(conn, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(budget.time, "monotonic", lambda: clock[0])
    original = sync._apply_one
    visits = []
    def slow(db, change, own, result):
        original(db, change, own, result)
        visits.append(change.seq)
        clock[0] += 0.1
    monkeypatch.setattr(sync, "_apply_one", slow)
    page = [_ch(i, "tasks", f"t_slow{i:016x}", _task_fields()) for i in range(1, 10)]
    applied = budget.apply_page(conn, page, new_cursor=10, observers=False)
    assert applied.applied == 9
    assert len(visits) > 9  # deadline really aborted and replayed a rolled-back prefix
    assert conn.execute("SELECT count(*) FROM tasks WHERE id LIKE 't_slow%'").fetchone()[0] == 9
    assert sync.sync_state(conn)["cursor"] == 10


def test_push_opportunities_between_batches_outside_writer(conn, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(budget.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(budget, "MAX_TRANSACTION_S", 1000)
    original = sync._apply_one
    def advance(db, change, own, result):
        original(db, change, own, result)
        clock[0] += 0.1
    monkeypatch.setattr(sync, "_apply_one", advance)
    sends = []
    def send():
        assert not conn.in_transaction
        sends.append((clock[0], sync.sync_state(conn)["cursor"]))
    page = [_ch(i, "tasks", f"t_push{i:016x}", _task_fields()) for i in range(1, 193)]
    with budget.push_service(send):
        budget.apply_page(conn, page, new_cursor=200, observers=False)
    assert len(sends) >= 2
    assert all(cursor < 200 for _, cursor in sends[:-1])
    times = [0, *[stamp for stamp, _ in sends], clock[0]]
    assert max(b - a for a, b in pairwise(times)) <= budget.PUSH_INTERVAL_S + budget.MAX_ROWS * 0.1


def test_batched_end_state_matches_atomic_page(conn, monkeypatch):
    page = [_ch(i, "tasks", f"t_equal{i:016x}", _task_fields()) for i in range(1, 90)]
    page += [_ch(100 + i, "tasks", f"t_equal{i:016x}", {"title": f"updated {i}"}, version=2)
             for i in range(1, 90)]
    sync.apply_remote_changes(conn, page, new_cursor=200, observers=False)
    expected = [tuple(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")]
    conn.execute("UPDATE kanban_sync_state SET applying=1")
    conn.execute("DELETE FROM tasks")
    conn.execute("UPDATE kanban_sync_state SET applying=0,cursor=0")
    # Tombstones are irrelevant to this controlled rewind of a disposable fixture.
    conn.execute("DELETE FROM kanban_sync_tombstones")
    budget.apply_page(conn, page, new_cursor=200, observers=False)
    assert [tuple(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")] == expected


def test_certified_stop_after_batch_cut_holds_before_prefix_hooks(conn, monkeypatch):
    from hermes_cli import kanban_containment_connections as connections
    from hermes_cli import kanban_containment_replica as replica
    from hermes_cli import kanban_db as kb
    from tests.hermes_cli import test_kanban_containment_replica as fixtures
    target, child, stop, _, hub = fixtures.scenario(conn)
    monkeypatch.setattr(budget, "MAX_ROWS", 1)
    page = [sync.RemoteChange(1, "tasks", target, "upsert", {"status": "ready"}, 10, "", "home"),
            fixtures.event(stop, hub, 2)]
    replica.certify_page(conn, hub, "fixture", page)
    observed = []
    def after_commit(db, result, board):
        observed.append((connections.held(db, target), kb.get_task(db, target).status))
        assert kb.claim_task(db, target) is None
    monkeypatch.setattr(sync, "_after_commit", after_commit)
    budget.apply_page(conn, page, new_cursor=2, board="fixture")
    assert observed == [(True, "triage"), (True, "triage")]
    assert kb.get_task(conn, child).status == "todo"


def test_small_legacy_page_without_sequence_is_atomic(conn):
    page = [_ch(0, "tasks", f"t_legacy{i:016x}", _task_fields()) for i in range(2)]
    assert budget.apply_page(conn, page, new_cursor=10, observers=False).applied == 2


def test_unordered_prefix_cannot_advance_cursor(conn, monkeypatch):
    monkeypatch.setattr(budget, "MAX_ROWS", 1)
    page = [_ch(0, "tasks", f"t_badseq{i:016x}", _task_fields()) for i in range(2)]
    with pytest.raises(ValueError, match="ordered sequence cursors"):
        budget.apply_page(conn, page, new_cursor=10, observers=False)
    assert sync.sync_state(conn)["cursor"] == 0
