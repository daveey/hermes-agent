"""Incremental ``_parked_scope`` memo over the P2 change cursor (card t_01a1d59e565b0b7c).

Every step is checked against the uncached reference query on a real sqlite board.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_move_parent_evidence as evidence
from hermes_cli import kanban_move_parent_scope as scope
from hermes_cli import kanban_task_change as tc
from hermes_cli import kanban_unused_grant
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    tc._reset_for_tests()
    scope._reset_for_tests()
    conn = kbc.connect(home / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()
        tc._reset_for_tests()
        scope._reset_for_tests()


@pytest.fixture
def evaluated(monkeypatch):
    """Task ids whose run/checkpoint was evaluated (the expensive per-child step)."""
    seen: list[str] = []
    real = kanban_unused_grant.latest_started_run

    def spy(conn, task_id):
        seen.append(task_id)
        return real(conn, task_id)

    monkeypatch.setattr(kanban_unused_grant, "latest_started_run", spy)
    return seen


def _write(conn, sql, params=()):
    with kbc.write_txn(conn):
        conn.execute(sql, params)


def _child(conn, title, parents, *, park=None):
    """A child linked to ``parents`` (ids absent from this file are foreign); ``park`` names its checkpoint."""
    task = kb.create_task(conn, title=title, assignee="w")
    with kbc.write_txn(conn):
        for parent in parents:
            conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent, task))
        if park is not None:
            _park(conn, task, park)
    return task


def _park(conn, task, named):
    run = conn.execute(
        "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) VALUES (?, 'blocked', 'blocked', 1, 2)",
        (task,)).lastrowid
    payload = {"kind": "dependency", "review_checkpoint": {"run_id": run, "parents": list(named)}}
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES "
                 "(?, ?, 'dependency_wait', ?, 2)", (task, run, json.dumps(payload)))


def _check(conn, expected):
    actual = evidence._parked_scope(conn)
    assert actual == set(expected)
    assert actual == evidence._parked_scope_uncached(conn)
    return actual


def test_unchanged_board_skips_the_expensive_query(board, evaluated):
    _child(board, "a", ["f1"], park=["f1"])
    _child(board, "b", ["f2"], park=["f2"])
    _check(board, {"f1", "f2"})
    assert len(evaluated) >= 2
    evaluated.clear()
    for _ in range(3):
        assert evidence._parked_scope(board) == {"f1", "f2"}
    assert evaluated == []


def test_one_changed_child_recomputes_only_that_child(board, evaluated):
    a = _child(board, "a", ["f1"], park=["f1"])
    _child(board, "b", ["f2"], park=["f2"])
    _check(board, {"f1", "f2"})
    evaluated.clear()
    _write(board, "UPDATE task_events SET payload = ? WHERE task_id = ? AND kind = 'dependency_wait'",
           (json.dumps({"kind": "dependency", "review_checkpoint": {
               "run_id": board.execute("SELECT id FROM task_runs WHERE task_id = ?", (a,)).fetchone()[0],
               "parents": ["f3"]}}), a))
    assert evidence._parked_scope(board) == {"f2", "f3"}
    assert evaluated == [a]
    assert evidence._parked_scope(board) == evidence._parked_scope_uncached(board)


def test_run_change_and_event_change_are_seen(board):
    a = _child(board, "a", ["f1"], park=["f1"])
    _check(board, {"f1"})
    _write(board, "UPDATE task_runs SET outcome = 'completed' WHERE task_id = ?", (a,))
    _check(board, set())
    _write(board, "UPDATE task_runs SET outcome = 'blocked' WHERE task_id = ?", (a,))
    _check(board, {"f1"})
    _write(board, "DELETE FROM task_events WHERE task_id = ? AND kind = 'dependency_wait'", (a,))
    _check(board, set())
    _write(board, "UPDATE tasks SET status = 'done' WHERE id = ?", (a,))
    _check(board, set())


def test_parent_imported_then_deleted_updates_child_membership(board, evaluated):
    _child(board, "a", ["t_remote"], park=["t_remote"])
    _child(board, "other", ["f9"], park=["f9"])
    _check(board, {"t_remote", "f9"})
    evaluated.clear()
    # The foreign parent is imported: the child no longer has a missing parent.
    _write(board, "INSERT INTO tasks (id, title, status, created_at) VALUES ('t_remote', 'p', 'todo', 1)")
    evaluated.clear()
    assert evidence._parked_scope(board) == {"f9"}
    assert evaluated == []  # no longer a member: nothing to evaluate, and nothing else was touched
    _check(board, {"f9"})
    # ...and deleted again: the child's membership returns without any write to the child itself.
    _write(board, "DELETE FROM tasks WHERE id = 't_remote'")
    _check(board, {"t_remote", "f9"})


def test_link_deletion_drops_the_child(board):
    a = _child(board, "a", ["f1"], park=["f1"])
    _check(board, {"f1"})
    _write(board, "DELETE FROM task_links WHERE child_id = ?", (a,))
    _check(board, set())
    _write(board, "INSERT INTO task_links (parent_id, child_id) VALUES ('f1', ?)", (a,))
    _check(board, {"f1"})


def test_shared_parents_survive_one_child_leaving(board):
    a = _child(board, "a", ["f1"], park=["f1", "shared"])
    b = _child(board, "b", ["f2"], park=["f2", "shared"])
    _check(board, {"f1", "f2", "shared"})
    _write(board, "UPDATE tasks SET status = 'archived' WHERE id = ?", (a,))
    _check(board, {"f2", "shared"})
    _write(board, "UPDATE tasks SET status = 'archived' WHERE id = ?", (b,))
    _check(board, set())


def test_rollback_cannot_poison_the_memo(board):
    a = _child(board, "a", ["f1"], park=["f1"])
    _check(board, {"f1"})
    board.execute("BEGIN IMMEDIATE")
    board.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (a,))
    assert evidence._parked_scope(board) == set()  # inside the transaction: bypass, sees its own write
    board.execute("ROLLBACK")
    assert evidence._parked_scope(board) == {"f1"}
    # A first-ever call inside a transaction stores nothing either.
    scope._reset_for_tests()
    board.execute("BEGIN IMMEDIATE")
    board.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (a,))
    assert evidence._parked_scope(board) == set()
    board.execute("ROLLBACK")
    assert scope._MEMO == {}
    _check(board, {"f1"})


@pytest.mark.parametrize("tamper", ["schema", "ino", "revision_ahead", "dropped"])
def test_untrusted_cursor_falls_back_to_full_recompute(board, evaluated, tamper):
    _child(board, "a", ["f1"], park=["f1"])
    _child(board, "b", ["f2"], park=["f2"])
    _check(board, {"f1", "f2"})
    (path, (cursor, *_rest)), = scope._MEMO.items()
    if tamper == "schema":
        cursor = dataclasses.replace(cursor, schema_version=cursor.schema_version + 1)
    elif tamper == "ino":
        cursor = dataclasses.replace(cursor, ino=cursor.ino + 1)
    elif tamper == "revision_ahead":
        cursor = dataclasses.replace(cursor, revision=cursor.revision + 100)
    if tamper == "dropped":
        board.execute("DROP TRIGGER IF EXISTS {}".format(board.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE '%task_events_insert'").fetchone()[0]))
    else:
        # A stale map the cursor can no longer vouch for must not be served.
        scope._MEMO[path] = (cursor, scope._Scope({"ghost": frozenset({"stale"})}), scope._Scope({}),
                             frozenset({"stale"}))
    evaluated.clear()
    _check(board, {"f1", "f2"})
    assert len(set(evaluated)) >= 2  # every child re-evaluated, not just the changed ones


def test_memo_is_bounded(board, monkeypatch):
    monkeypatch.setattr(scope, "_MAX", 2)
    for name in ("p1", "p2", "p3"):
        scope._MEMO[name] = (None, {})
    _child(board, "a", ["f1"], park=["f1"])
    evidence._parked_scope(board)
    assert len(scope._MEMO) <= 2


def test_refcounted_union_keeps_shared_parents_and_reuses_the_union():
    s = scope._Scope({"a": frozenset({"p", "q"}), "b": frozenset({"p"})})
    first = s.union
    assert first == {"p", "q"}
    assert s.set({"b": frozenset()}) is False  # "a" still names p: membership unchanged
    assert s.union is first  # same object, so callers can skip an O(scope) difference
    assert s.set({"a": frozenset({"q"})}) is True  # last reference to p gone
    assert s.union == {"q"}
    assert s.set({"c": frozenset({"q"}), "a": frozenset()}) is False  # c takes over q before a drops it
    assert s.set({"c": frozenset({"r"})}) is True
    assert s.union == {"r"} and s.counts == {"r": 1}


def test_refcounted_union_matches_a_fresh_union_under_random_updates():
    import random

    rng = random.Random(7)
    parents = [f"p{i}" for i in range(6)]
    s = scope._Scope({})
    model: dict = {}
    for _ in range(2000):
        updates = {f"t{rng.randrange(8)}": frozenset(rng.sample(parents, rng.randrange(3)))
                   for _ in range(rng.randrange(1, 4))}
        before = s.union
        moved = s.set(updates)
        model.update(updates)
        expected = scope._union({k: v for k, v in model.items() if v})
        assert s.union == expected
        assert moved == (expected != before)
        assert s.by_task == {k: v for k, v in model.items() if v}
