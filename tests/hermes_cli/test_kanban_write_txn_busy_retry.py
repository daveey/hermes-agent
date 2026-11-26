"""write_txn BUSY-retry behaviour.

These tests target the transaction boundary (BEGIN IMMEDIATE / COMMIT) only.
On unmodified main write_txn has no application-level retry, so the
"transient BUSY is absorbed" and "persistent BUSY is bounded" cases fail until
the fix lands. No disk DB is touched: a fake connection records and replays
scripted boundary outcomes. Schema queries use an isolated in-memory SQLite DB.
"""

import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


class _FakeConn:
    """Records execute() calls and replays a scripted result per SQL statement.

    script maps an uppercased SQL prefix to a list of outcomes consumed in
    order. An outcome is either an Exception (raised) or None (success).
    """

    def __init__(self, script, schema):
        self._script = {k: list(v) for k, v in script.items()}
        self.calls = []
        self._schema = schema

    def execute(self, sql, *args):
        self.calls.append(sql)
        key = sql.strip().split()[0].upper()
        outcomes = self._script.get(key)
        if outcomes:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
        # Real empty SQLite answers schema reads; only boundary outcomes are fake.
        # Returning None here breaks the DB-API cursor contract before retry proof.
        if key in {"SELECT", "PRAGMA"}:
            return self._schema.execute(sql, *args)

    def count(self, prefix):
        prefix = prefix.upper()
        return sum(1 for c in self.calls if c.strip().upper().startswith(prefix))


def _busy():
    return sqlite3.OperationalError("database is locked")


def _other():
    return sqlite3.OperationalError("no such table: tasks")


@pytest.fixture(autouse=True)
def _no_file_check(monkeypatch):
    # Isolate the boundary behaviour from the post-commit invariant.
    monkeypatch.setattr(kbc, "_check_file_length_invariant", lambda conn: None)


@pytest.fixture
def schema():
    conn = sqlite3.connect(':memory:')
    try:
        yield conn
    finally:
        conn.close()


def test_retry_sleep_respects_floor(monkeypatch, schema):
    # The jitter has a floor so a retry can't busy-spin back into the collision.
    slept = []
    monkeypatch.setattr(kb.time, "sleep", lambda s: slept.append(s))
    conn = _FakeConn({"BEGIN": [_busy(), _busy(), None]}, schema)
    with kb.write_txn(conn):
        pass
    assert slept
    assert all(s >= kbc._BUSY_RETRY_MIN_S for s in slept)
    assert all(s <= kbc._BUSY_RETRY_MAX_S for s in slept)


def test_transient_busy_at_begin_is_absorbed(schema):
    conn = _FakeConn({"BEGIN": [_busy(), None]}, schema)
    with kb.write_txn(conn):
        pass
    assert conn.count("BEGIN") == 2
    assert conn.count("COMMIT") == 1


def test_persistent_busy_at_commit_rolls_back(schema):
    # Exhausted COMMIT leaves the txn open; write_txn must ROLLBACK before
    # re-raising so the connection isn't poisoned for the next transaction.
    conn = _FakeConn({"COMMIT": [_busy()] * 50}, schema)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        with kb.write_txn(conn):
            pass
    assert conn.count("ROLLBACK") == 1
