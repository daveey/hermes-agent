"""Queued evidence scope stays differential with the uncached SQL reference."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_move_parent_evidence as evidence
from hermes_cli import kanban_move_parent_scope as scope
from tests.hermes_cli import test_kanban_move_parent_scope as helpers

board = helpers.board


def _move(conn, task, payload=None):
    with helpers.kbc.write_txn(conn):
        return conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'moved', ?, 1)",
            (task, json.dumps(payload if payload is not None else {"queued": True, "checkpoint": "ok"})),
        ).lastrowid


def test_queued_scope_includes_local_and_foreign_parents_without_a_park(board):
    local = helpers.kb.create_task(board, title="local")
    task = helpers._child(board, "queued", [local, "foreign"])
    _move(board, task)
    assert scope.parked_scope(board) == set()
    assert scope.evidence_scope(board) == {local, "foreign"}
    assert scope.evidence_scope(board) == evidence._evidence_scope_uncached(board)


def _check(conn, expected):
    actual = scope.evidence_scope(conn)
    assert actual == set(expected)
    assert actual == evidence._evidence_scope_uncached(conn)
    return actual


def test_moved_task_id_update_invalidates_both_tasks(board):
    a = helpers._child(board, "a", ["a-parent"])
    b = helpers._child(board, "b", ["b-parent"])
    event = _move(board, a)
    _check(board, {"a-parent"})
    helpers._write(board, "UPDATE task_events SET task_id = ? WHERE id = ?", (b, event))
    _check(board, {"b-parent"})


def test_lower_id_insert_cannot_supersede_latest_moved(board):
    task = helpers._child(board, "a", ["foreign"])
    event = _move(board, task, {"queued": False, "checkpoint": "ok"})
    _check(board, set())
    helpers._write(board, "INSERT INTO task_events (id, task_id, kind, payload, created_at) "
                   "VALUES (-100, ?, 'moved', ?, 999)",
                   (task, json.dumps({"queued": True, "checkpoint": "ok"})))
    _check(board, set())
    helpers._write(board, "DELETE FROM task_events WHERE id = ?", (event,))
    _check(board, {"foreign"})


def test_moved_mutation_sequence_uses_max_id_and_old_and_new_task_ids(board):
    a = helpers._child(board, "a", ["a-parent"])
    b = helpers._child(board, "b", ["b-parent"])
    lower = _move(board, a)
    _check(board, {"a-parent"})
    latest = _move(board, a, {"queued": False, "checkpoint": "ok"})
    _check(board, set())
    helpers._write(board, "UPDATE task_events SET created_at = 999 WHERE id = ?", (lower,))
    _check(board, set())  # timestamps/lower-id changes do not override MAX(id)
    helpers._write(board, "UPDATE task_events SET payload = ? WHERE id = ?",
                   (json.dumps({"queued": True, "checkpoint": " ok "}), latest))
    _check(board, {"a-parent"})
    helpers._write(board, "UPDATE task_events SET task_id = ? WHERE id = ?", (b, latest))
    _check(board, {"a-parent", "b-parent"})
    helpers._write(board, "UPDATE task_events SET kind = 'comment' WHERE id = ?", (lower,))
    _check(board, {"b-parent"})
    helpers._write(board, "UPDATE task_events SET kind = 'moved' WHERE id = ?", (lower,))
    _check(board, {"a-parent", "b-parent"})
    helpers._write(board, "DELETE FROM task_events WHERE id = ?", (latest,))
    _check(board, {"a-parent"})
    helpers._write(board, "DELETE FROM task_events WHERE id = ?", (lower,))
    _check(board, set())


@pytest.mark.parametrize("raw,valid", [
    ('{"queued":true,"checkpoint":" ok "}', True),
    ('{"queued":1,"checkpoint":"ok"}', False),
    ('{"queued":"true","checkpoint":"ok"}', False),
    ('{"checkpoint":"ok"}', False),
    ('{"queued":true,"checkpoint":" \\t\\n"}', False),
    ('{"queued":true,"checkpoint":1}', False),
    ('{"queued":true,"checkpoint":null}', False),
    ('{"queued":true}', False), ('[]', False), ('null', False), ('invalid', False),
])
def test_queued_payload_predicate(board, raw, valid):
    task = helpers._child(board, "a", ["foreign"])
    event = _move(board, task)
    _check(board, {"foreign"})
    helpers._write(board, "UPDATE task_events SET payload = ? WHERE id = ?", (raw, event))
    _check(board, {"foreign"} if valid else set())


def test_queued_status_links_parent_and_child_lifecycle(board):
    task = helpers._child(board, "a", ["remote"])
    _move(board, task)
    _check(board, {"remote"})
    for status in ("done", "ready", "archived", "blocked", "todo"):
        helpers._write(board, "UPDATE tasks SET status = ? WHERE id = ?", (status, task))
        _check(board, set() if status in ("done", "archived") else {"remote"})
    helpers._write(board, "INSERT INTO tasks (id, title, status, created_at) VALUES ('remote', 'p', 'todo', 1)")
    _check(board, {"remote"})  # queued includes local parents too
    helpers._write(board, "DELETE FROM tasks WHERE id = 'remote'")
    _check(board, {"remote"})
    helpers._write(board, "UPDATE task_links SET parent_id = 'other' WHERE child_id = ?", (task,))
    _check(board, {"other"})
    helpers._write(board, "DELETE FROM task_links WHERE child_id = ?", (task,))
    _check(board, set())
    helpers._write(board, "INSERT INTO task_links (parent_id, child_id) VALUES ('new', ?)", (task,))
    _check(board, {"new"})
    helpers._write(board, "DELETE FROM tasks WHERE id = ?", (task,))
    _check(board, set())


def test_changed_row_and_parent_do_not_rescan_queued_children(board, monkeypatch):
    parent = helpers.kb.create_task(board, title="parent")
    a = helpers._child(board, "a", [parent, "foreign"])
    b = helpers._child(board, "b", ["other"])
    _move(board, a)
    _move(board, b)
    _check(board, {parent, "foreign", "other"})
    seen = []
    real = evidence._queued_checkpoint
    monkeypatch.setattr(evidence, "_queued_checkpoint", lambda payload: (seen.append(payload), real(payload))[1])
    queries = []
    board.set_trace_callback(queries.append)
    try:
        helpers._write(board, "UPDATE tasks SET title = 'changed' WHERE id = ?", (parent,))
        assert scope.evidence_scope(board) == {parent, "foreign", "other"}
        assert seen == []  # parked expands children of changed parents; queued must not
        helpers._write(board, "UPDATE tasks SET title = 'changed' WHERE id = ?", (a,))
        assert scope.evidence_scope(board) == {parent, "foreign", "other"}
        assert len(seen) == 1
        moved_queries = [q for q in queries if "SELECT e.task_id, e.payload" in q]
        assert moved_queries and all("AND t.id IN (" in q for q in moved_queries)
        seen.clear()
        assert scope.evidence_scope(board) == {parent, "foreign", "other"}
        assert evidence.prepare(board, board, []) == ([], [])
        assert seen == []
    finally:
        board.set_trace_callback(None)


def test_queued_rollback_and_fresh_return_value(board):
    a = helpers._child(board, "a", ["foreign"])
    event = _move(board, a)
    returned = _check(board, {"foreign"})
    assert isinstance(returned, frozenset)  # callers cannot mutate the memo's union through the return value
    assert scope.evidence_scope(board) == {"foreign"}
    held = dict(scope._MEMO)
    board.execute("BEGIN IMMEDIATE")
    board.execute("DELETE FROM task_events WHERE id = ?", (event,))
    assert scope.evidence_scope(board) == set()
    assert scope._MEMO == held
    board.execute("ROLLBACK")
    _check(board, {"foreign"})


def test_both_scopes_share_snapshot_and_catch_up_after_concurrent_write(board, monkeypatch):
    task = helpers._child(board, "a", ["parked"], park=["parked"])
    event = _move(board, task)
    writer = helpers.kbc.connect(Path(helpers.tc._main_path(board)))
    real = scope._full
    def racing(conn):
        result = real(conn)
        helpers._write(writer, "UPDATE task_events SET payload = '{}' WHERE id = ?", (event,))
        return result
    monkeypatch.setattr(scope, "_full", racing)
    try:
        assert scope.evidence_scope(board) == {"parked"}
        # Inspect each half: the queued rows must belong to the same old snapshot.
        held = next(iter(scope._MEMO.values()))
        assert held[2].by_task == {task: frozenset({"parked"})}
        monkeypatch.setattr(scope, "_full", real)
        assert scope.evidence_scope(board) == {"parked"}
        assert next(iter(scope._MEMO.values()))[2].by_task == {}
    finally:
        writer.close()


def test_exception_does_not_publish_either_half(board, monkeypatch):
    task = helpers._child(board, "a", ["foreign"])
    _move(board, task)
    _check(board, {"foreign"})
    assert scope._MEMO
    helpers._write(board, "UPDATE tasks SET title = 'changed' WHERE id = ?", (task,))
    def fail(*args):
        raise sqlite3.OperationalError("queued read failed")
    real = scope._queued
    monkeypatch.setattr(scope, "_queued", fail)
    with pytest.raises(sqlite3.OperationalError, match="queued read failed"):
        scope.evidence_scope(board)
    # The entry is checked out while it is updated in place: a failure drops it (nothing half-updated is
    # published), and the next call recomputes in full.
    assert scope._MEMO == {}
    assert not board.in_transaction
    monkeypatch.setattr(scope, "_queued", real)
    _check(board, {"foreign"})


def test_queued_null_status_uses_sql_not_in_semantics():
    # Production schema rejects NULL, but legacy nullable boards retain SQL's exclusion.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript("CREATE TABLE tasks (id TEXT, status TEXT); "
                           "CREATE TABLE task_links (parent_id TEXT, child_id TEXT); "
                           "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT);")
        conn.execute("INSERT INTO tasks VALUES ('a', NULL)")
        conn.execute("INSERT INTO task_links VALUES ('foreign', 'a')")
        conn.execute("INSERT INTO task_events VALUES (1, 'a', 'moved', ?)",
                     (json.dumps({"queued": True, "checkpoint": "ok"}),))
        assert scope._queued(conn) == {}
        assert evidence._queued_scope_uncached(conn) == set()
    finally:
        conn.close()


@pytest.mark.parametrize("fallback", ["full", "no_cursor", "error"])
def test_queued_cursor_fallback_recomputes_and_drops_untrusted_memo(board, monkeypatch, fallback):
    task = helpers._child(board, "a", ["foreign"])
    event = _move(board, task)
    _check(board, {"foreign"})
    helpers._write(board, "DELETE FROM task_events WHERE id = ?", (event,))
    real = helpers.tc.changed_since
    def changed(conn, cursor):
        if fallback == "error":
            raise sqlite3.OperationalError("no cursor")
        _, fresh = real(conn, cursor)
        return (helpers.tc.FULL, fresh) if fallback == "full" else (set(), None)
    monkeypatch.setattr(helpers.tc, "changed_since", changed)
    _check(board, set())
    assert bool(scope._MEMO) == (fallback == "full")
