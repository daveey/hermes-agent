"""Real SQLite transaction composition; no HOME authority is installed by these tests."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_promote as promote, kanban_spend_budget as budget
from tests.hermes_cli import test_kanban_spend_budget as helpers

kanban_home = helpers.kanban_home


@pytest.fixture
def card(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Atomic release transaction fixture", assignee="default")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))
        yield conn, tid


def snapshot(conn):
    return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("tasks", "task_events", "task_comments")}


def override(conn, tid):
    return budget.set_override_in_txn(conn, tid, runs=12, cache_read_tokens=0, by="original-owner",
                                       reason="Finite original goal allowance", writer_kind="agent")


@pytest.mark.parametrize("operation", ["budget", "promote"])
def test_body_requires_outer_transaction_without_writes(card, operation):
    conn, tid = card
    before = snapshot(conn)
    with pytest.raises(RuntimeError, match="transaction"):
        override(conn, tid) if operation == "budget" else promote.promote_in_txn(conn, tid, actor="original-owner")
    assert snapshot(conn) == before
    assert not conn.in_transaction


def test_combined_commit_has_single_writer_and_attributed_budget(card):
    conn, tid = card
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        with kb.write_txn(conn):
            payload = override(conn, tid)
            assert promote.promote_in_txn(conn, tid, actor="original-owner") == (True, None)
            assert conn.in_transaction
    finally:
        conn.set_trace_callback(None)
    assert sum(sql.startswith("BEGIN IMMEDIATE") for sql in statements) == 1
    assert sum(sql == "COMMIT" for sql in statements) == 1
    assert kb.get_task(conn, tid).status == "ready"
    assert budget.card_override(conn, tid) == payload
    assert payload["runs"] == 12 and payload["writer_kind"] == "agent"
    comment = conn.execute("SELECT body FROM task_comments WHERE id=?", (payload["decision_comment"]["comment_id"],)).fetchone()
    assert "runs 12" in comment[0]
    assert len([e for e in kb.list_events(conn, tid) if e.kind == "promoted_manual"]) == 1


@pytest.mark.parametrize("failure", ["parent", "precondition", "after_promotion"])
def test_later_refusal_or_failure_rolls_back_budget_comment_events_and_status(card, failure):
    conn, tid = card
    if failure == "parent":
        parent = kb.create_task(conn, title="Unfinished prerequisite", assignee="default")
        kb.link_tasks(conn, parent, tid)
    before = snapshot(conn)
    with pytest.raises(RuntimeError, match="abort composed application"):
        with kb.write_txn(conn):
            override(conn, tid)
            ok, why = promote.promote_in_txn(conn, tid, actor="original-owner",
                                            precondition=(lambda c: "new hold") if failure == "precondition" else None)
            if failure == "parent":
                assert not ok and "unsatisfied parent" in why
            elif failure == "precondition":
                assert not ok and why == "new hold"
            else:
                assert ok and kb.get_task(conn, tid).status == "ready"
            raise RuntimeError("abort composed application")
    assert snapshot(conn) == before
    assert not conn.in_transaction


def test_public_promote_rechecks_parent_added_at_writer_acquisition(card, monkeypatch):
    conn, tid = card
    parent = kb.create_task(conn, title="Concurrent prerequisite", assignee="default")
    original = kb.write_txn
    from contextlib import contextmanager

    @contextmanager
    def acquire(c, **kwargs):
        with original(c, **kwargs):
            c.execute("INSERT INTO task_links(parent_id,child_id) VALUES (?,?)", (parent, tid))
            yield

    monkeypatch.setattr(kb, "write_txn", acquire)
    before_events = [tuple(r) for r in conn.execute("SELECT * FROM task_events WHERE task_id=?", (tid,))]
    ok, why = kb.promote_task(conn, tid, actor="original-owner")
    assert not ok and "unsatisfied parent" in why
    assert kb.get_task(conn, tid).status == "todo"
    assert [tuple(r) for r in conn.execute("SELECT * FROM task_events WHERE task_id=?", (tid,))] == before_events


def test_public_budget_invalid_argument_does_not_acquire_writer(card, monkeypatch):
    conn, tid = card
    def forbidden(*a, **kw):
        pytest.fail("invalid public budget acquired a writer")
    monkeypatch.setattr(kb, "write_txn", forbidden)
    with pytest.raises(ValueError, match="non-negative integer"):
        budget.set_override(conn, tid, runs=True, by="owner", reason="invalid")


def test_promotion_body_cas_and_precondition_preserved(card):
    conn, tid = card
    before = snapshot(conn)
    with pytest.raises(RuntimeError, match="rollback"):
        with kb.write_txn(conn):
            def change_status(c):
                c.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
            ok, why = promote.promote_in_txn(conn, tid, actor="original-owner", precondition=change_status)
            assert not ok and "status changed" in why
            raise RuntimeError("rollback")
    assert snapshot(conn) == before


@pytest.mark.parametrize("kind", [budget.OVERRIDE_EVENT, "promoted_manual"])
def test_real_event_insert_failure_rolls_back_all_composed_effects(card, kind):
    import sqlite3
    conn, tid = card
    with kb.write_txn(conn):
        conn.execute(f"CREATE TEMP TRIGGER refuse_composed_event BEFORE INSERT ON task_events "
                     f"WHEN NEW.kind='{kind}' BEGIN SELECT RAISE(ABORT,'injected event failure'); END")
    before = snapshot(conn)
    with pytest.raises(sqlite3.IntegrityError, match="injected event failure"):
        with kb.write_txn(conn):
            override(conn, tid)
            assert promote.promote_in_txn(conn, tid, actor="original-owner")[0]
    assert snapshot(conn) == before


def test_composed_budget_retains_real_unread_fence_and_explicit_ack(card):
    from tests.hermes_cli import test_kanban_spend_override_fence as fence_helpers
    from hermes_cli.kanban_release_comments import UnreadComments
    conn, tid = card
    fence_helpers._stop(conn, tid)
    first = budget.set_override(conn, tid, runs=1, by="first-owner", reason="bounded prior decision")
    before = snapshot(conn)
    with pytest.raises(UnreadComments):
        with kb.write_txn(conn):
            override(conn, tid)
    assert snapshot(conn) == before
    with kb.write_txn(conn):
        payload = budget.set_override_in_txn(conn, tid, runs=4, by="owner", reason="read prior decision",
                                              seen=first["decision_comment"]["comment_id"], writer_kind="agent")
        assert promote.promote_in_txn(conn, tid, actor="owner")[0]
    assert payload["episode"] == first["episode"]
    assert kb.get_task(conn, tid).status == "ready"


@pytest.mark.parametrize("status", ["ready", "scheduled"])
def test_composed_ready_exception_rechecks_pending_after_precondition(card, status):
    from tests.hermes_cli import test_kanban_spend_override_fence as fence_helpers
    conn, tid = card
    fence_helpers._stop(conn, tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    before = snapshot(conn)
    with pytest.raises(RuntimeError, match="rollback"):
        with kb.write_txn(conn):
            def settle(c):
                kb._append_event(c, tid, "promoted_manual", {"actor": "another-decider"})
            ok, why = promote.promote_in_txn(conn, tid, actor="owner", precondition=settle)
            assert not ok and "spend decision changed" in why
            raise RuntimeError("rollback")
    assert snapshot(conn) == before


def test_composed_expected_status_refuses_changed_eligible_status(card):
    conn, tid = card
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
    before = snapshot(conn)
    with kb.write_txn(conn):
        ok, why = promote.promote_in_txn(conn, tid, actor="owner", expected_status="todo")
        assert not ok and "status changed" in why
    assert snapshot(conn) == before


def test_public_budget_payload_and_event_contract_preserved(card):
    conn, tid = card
    payload = budget.set_override(conn, tid, runs=7, cache_read_tokens=90, by="owner", reason="bounded",
                                  writer_kind="agent", cache_read_writer_kind="human", runs_writer_kind="unknown")
    saved = next(e for e in kb.list_events(conn, tid) if e.kind == budget.OVERRIDE_EVENT)
    assert saved.payload == payload
    assert payload["cache_read_writer_kind"] == "human" and payload["runs_writer_kind"] == "unknown"
    assert payload["by"] == "owner" and payload["reason"] == "bounded"
