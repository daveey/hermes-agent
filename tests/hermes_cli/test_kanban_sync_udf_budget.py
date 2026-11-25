"""Real containment trigger callbacks must retain SQLite interrupt provenance."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from hermes_cli import kanban_containment_functions as functions
from hermes_cli import kanban_sync_client as client

import pytest

from hermes_cli import kanban_containment_guard as guard
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_apply_budget as budget
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.test_kanban_sync_apply import _ch, _task_fields


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    yield from support.synced_conn(home)


def seed(conn):
    task = "t_udfbudget00000001"
    sync.apply_remote_changes(conn, [_ch(1, "tasks", task, _task_fields())],
                              new_cursor=1, observers=False)
    # Closed receipts without a stop do not hold the task. Enough real history
    # makes the production pending() query cross the 1000-opcode progress cut.
    guard.install(conn)
    conn.execute("BEGIN IMMEDIATE")
    with guard.authorized_write(conn, task):
        conn.executemany("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                         [(task, "containment_closed", "{}", 1)] * 512)
    conn.commit()
    assert not guard.pending(conn, task)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='containment_task_update'").fetchone()
    return task


def expire_inside_history(conn, monkeypatch, clock, *, armed=lambda: True):
    seen = []
    monkeypatch.setattr(budget.time, "monotonic", lambda: clock[0])

    def trace(sql):
        # SQLite prefixes statements executed from a Python callback with '--'.
        # Only advance the clock once INSIDE the real containment UDF's query;
        # do not replace the UDF, progress handler, connection or SQL engine.
        if (not seen and armed() and budget._DEADLINE.get() is not None
                and sql.startswith("-- SELECT uid,task_id,kind,payload FROM task_events")):
            seen.append(sql)
            clock[0] = budget._DEADLINE.get() + 1

    conn.set_trace_callback(trace)
    return seen


def test_containment_udf_interrupt_is_budget_exceeded(conn, monkeypatch):
    task = seed(conn)
    clock = [0.0]
    seen = expire_inside_history(conn, monkeypatch, clock)
    token = budget._LIMIT.set(budget.MAX_TRANSACTION_S)
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            with budget.transaction_budget(conn):
                conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (task,))
        assert seen, "deadline did not expire inside the production containment callback"
        assert isinstance(caught.value, budget.ApplyBudgetExceeded), str(caught.value)
        assert str(caught.value.__cause__) == "user-defined function raised exception"
    finally:
        conn.set_trace_callback(None)
        conn.rollback()
        budget._LIMIT.reset(token)
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()[0] == "ready"
    assert budget._DEADLINE.get() is None
    assert conn.execute("SELECT 42").fetchone()[0] == 42


def test_containment_udf_error_before_deadline_is_not_budget_exceeded(conn, monkeypatch):
    task = seed(conn)
    monkeypatch.setattr(budget.time, "monotonic", lambda: 0.0)
    denied = []

    def authorize(action, table, column, database, source):
        if action == sqlite3.SQLITE_READ and table == "task_events":
            denied.append((table, column))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    token = budget._LIMIT.set(budget.MAX_TRANSACTION_S)
    conn.execute("BEGIN IMMEDIATE")
    conn.set_authorizer(authorize)
    try:
        with pytest.raises(sqlite3.OperationalError, match="user-defined function raised exception") as caught:
            with budget.transaction_budget(conn):
                conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (task,))
        assert denied
        assert type(caught.value) is sqlite3.OperationalError
        assert caught.value.__cause__ is None
    finally:
        conn.set_authorizer(None)
        conn.rollback()
        budget._LIMIT.reset(token)


def test_page_halves_after_containment_udf_interrupt(conn, monkeypatch):
    task = seed(conn)
    page = [_ch(i, "tasks", task if i == 18 else f"t_udfpage{i:016x}",
                _task_fields(status="todo"), version=2) for i in range(2, 42)]
    batches = []
    original = sync.apply_remote_changes

    def apply(db, changes, **kwargs):
        batches.append((len(changes), kwargs["new_cursor"]))
        return original(db, changes, **kwargs)

    monkeypatch.setattr(sync, "apply_remote_changes", apply)
    seen = expire_inside_history(conn, monkeypatch, [0.0], armed=lambda: bool(batches) and batches[-1][0] == 32)
    try:
        try:
            result = budget.apply_page(conn, page, new_cursor=50, observers=False)
        except sqlite3.OperationalError as exc:
            pytest.fail(f"page did not retry a containment UDF interrupt: {exc}")
    finally:
        conn.set_trace_callback(None)
    assert seen
    assert [size for size, _ in batches] == [32, 16, 16, 8]
    assert [cursor for _, cursor in batches] == [33, 17, 33, 50]
    assert result.applied == len(page)
    assert sync.sync_state(conn)["cursor"] == 50
    assert sync.sync_state(conn)["applying"] == 0
    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM tasks WHERE id LIKE 't_udfpage%'").fetchone()[0] == 39
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()[0] == "todo"


def test_round_warning_names_inner_trigger_exception(conn, tmp_path, monkeypatch, caplog):
    task = seed(conn)
    original = ValueError("containment diagnostic fixture")

    def broken():
        raise original

    functions.register(conn, "kanban_containment_diagnostic", 0, broken)
    conn.execute("CREATE TRIGGER diagnostic_update BEFORE UPDATE ON tasks "
                 "BEGIN SELECT kanban_containment_diagnostic(); END")
    monkeypatch.setattr(client, "publish_pickup_if_changed", lambda hub: None)
    from hermes_cli import kanban_source_clones
    monkeypatch.setattr(kanban_source_clones, "attest_if_due", lambda hub: None)

    def round_(*args):
        conn.execute("UPDATE tasks SET title='changed' WHERE id=?", (task,))

    monkeypatch.setattr(client, "sync_board", round_)
    hub = SimpleNamespace(ident=SimpleNamespace(gateway="fixture"))
    with caplog.at_level("WARNING", logger=client.__name__):
        result = client.sync_all_once(hub, boards=[("diagnostic", tmp_path / "board.db")])
    assert result[0].error == "OperationalError: user-defined function raised exception"
    assert "inner: ValueError: containment diagnostic fixture in kanban_containment_diagnostic" in caplog.text
    # A different, non-UDF failure must not reuse the callback diagnostic.
    caplog.clear()

    def other(*args):
        raise sqlite3.OperationalError("unrelated failure")

    monkeypatch.setattr(client, "sync_board", other)
    client.sync_all_once(hub, boards=[("diagnostic", tmp_path / "board.db")])
    assert "inner:" not in caplog.text
    caplog.clear()

    def opaque(*args):
        raise sqlite3.OperationalError("user-defined function raised exception")

    monkeypatch.setattr(client, "sync_board", opaque)
    client.sync_all_once(hub, boards=[("diagnostic", tmp_path / "board.db")])
    assert "inner:" not in caplog.text
    # The wrapper itself must re-raise the original exception, not a replacement.
    callback = functions._FUNCTIONS[(id(conn), "kanban_containment_diagnostic", 0)]
    with pytest.raises(ValueError) as caught:
        callback()
    assert caught.value is original
    functions.clear_error()


def test_round_warning_omits_handled_inner_error_for_non_udf_failure(conn, tmp_path, monkeypatch, caplog):
    task = seed(conn)

    def broken():
        raise ValueError("handled containment error")

    functions.register(conn, "kanban_containment_handled", 0, broken)
    conn.execute("CREATE TRIGGER handled_update BEFORE UPDATE ON tasks "
                 "BEGIN SELECT kanban_containment_handled(); END")
    monkeypatch.setattr(client, "publish_pickup_if_changed", lambda hub: None)
    from hermes_cli import kanban_source_clones
    monkeypatch.setattr(kanban_source_clones, "attest_if_due", lambda hub: None)
    handled = []

    def round_(*args):
        with pytest.raises(sqlite3.OperationalError, match="user-defined function raised exception"):
            conn.execute("UPDATE tasks SET title='changed' WHERE id=?", (task,))
        handled.append(True)
        raise OSError("transport failed after handled trigger")

    monkeypatch.setattr(client, "sync_board", round_)
    hub = SimpleNamespace(ident=SimpleNamespace(gateway="fixture"))
    with caplog.at_level("WARNING", logger=client.__name__):
        result = client.sync_all_once(hub, boards=[("non-udf", tmp_path / "board.db")])
    assert handled == [True]
    assert result[0].error == "OSError: transport failed after handled trigger"
    assert "round for non-udf failed" in caplog.text
    assert "; inner:" not in caplog.text


def test_round_warning_clears_handled_inner_error_between_boards(conn, tmp_path, monkeypatch, caplog):
    task = seed(conn)

    def broken():
        raise ValueError("earlier board containment error")

    def plain_broken():
        raise RuntimeError("plain callback error")

    functions.register(conn, "kanban_containment_earlier", 0, broken)
    conn.create_function("plain_diagnostic", 0, plain_broken)
    conn.execute("CREATE TRIGGER earlier_update BEFORE UPDATE OF title ON tasks "
                 "BEGIN SELECT kanban_containment_earlier(); END")
    conn.execute("CREATE TRIGGER plain_update BEFORE UPDATE OF status ON tasks "
                 "BEGIN SELECT plain_diagnostic(); END")
    monkeypatch.setattr(client, "publish_pickup_if_changed", lambda hub: None)
    from hermes_cli import kanban_source_clones
    monkeypatch.setattr(kanban_source_clones, "attest_if_due", lambda hub: None)
    visited = []

    def round_(slug, path, hub, gateway):
        visited.append(slug)
        if slug == "earlier":
            with pytest.raises(sqlite3.OperationalError, match="user-defined function raised exception"):
                conn.execute("UPDATE tasks SET title='changed' WHERE id=?", (task,))
            return client.RoundResult(board=slug, mode="fixture")
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (task,))

    monkeypatch.setattr(client, "sync_board", round_)
    hub = SimpleNamespace(ident=SimpleNamespace(gateway="fixture"))
    with caplog.at_level("WARNING", logger=client.__name__):
        result = client.sync_all_once(hub, boards=[("earlier", tmp_path / "earlier.db"),
                                                  ("later", tmp_path / "later.db")])
    assert visited == ["earlier", "later"]
    assert not result[0].error
    assert result[1].error == "OperationalError: user-defined function raised exception"
    assert "round for earlier failed" not in caplog.text
    assert "round for later failed" in caplog.text
    assert "; inner:" not in caplog.text
