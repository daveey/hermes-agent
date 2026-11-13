"""[daveey] t_01a1d5ada30e3458: retry clauses of the schema-memo data heals that no test reached (PR #2214).

Post-merge mutants of #2214 (check t_01a1d59498c0a89a) survived on three clauses: a NULL-``active`` heal
stopped by a lock must report False (or it waits a full ``HEAL_INTERVAL_S``, not ``HEAL_RETRY_S``); the full
init must keep the delegate-marker heal's failure when the NULL-``active`` heal after it succeeds; and a heal
that succeeds must end the failure streak, so the next failure warns again.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

import hermes_state_schema
from hermes_state import SessionDB
from tests.hermes_state.test_schema_init_memo_heals import (
    _legacy_nullable_active_store,
    _marker,
    _polluted_store_with_an_unindexed_message,
)


@pytest.fixture(autouse=True)
def fresh_memo():
    import hermes_state_schema_memo

    hermes_state_schema_memo.clear()
    yield
    hermes_state_schema_memo.clear()


@pytest.fixture
def reconciles(monkeypatch):
    seen = {"n": 0}
    real = hermes_state_schema.SessionSchemaMixin._reconcile_columns

    def spy(self, cursor):
        seen["n"] += 1
        return real(self, cursor)

    monkeypatch.setattr(hermes_state_schema.SessionSchemaMixin, "_reconcile_columns", spy)
    return seen


def _memo():
    import hermes_state_schema_memo

    return hermes_state_schema_memo


def _advance(monkeypatch, seconds):
    """Move the memo's monotonic clock *seconds* past wherever it reads now."""
    memo = _memo()
    now = memo.time.monotonic()
    monkeypatch.setattr(memo.time, "monotonic", lambda: now + seconds)


def _heal_warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and "heal did not complete" in r.getMessage()]


class _LockedNullActiveUpdate:
    """The heal's own cursor, except that its UPDATE meets a lock (as a sibling writer's would)."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, *args):
        if sql.lstrip().startswith("UPDATE messages SET active = 1"):
            raise sqlite3.OperationalError("database is locked")
        return self._cursor.execute(sql, *args)


def _lock_null_active_update(monkeypatch):
    """Run the real ``_heal_null_active_rows`` (its except clause is what is under test) with its UPDATE
    locked while the returned ``{"on": ...}`` switch is on."""
    real = hermes_state_schema.SessionSchemaMixin._heal_null_active_rows
    locked = {"on": True}

    def maybe_locked(self, cursor):
        return real(self, _LockedNullActiveUpdate(cursor) if locked["on"] else cursor)

    monkeypatch.setattr(hermes_state_schema.SessionSchemaMixin, "_heal_null_active_rows", maybe_locked)
    return locked


def _hide_a_turn(path, content):
    con = sqlite3.connect(path)  # an older-build writer on the same store omits ``active``
    con.execute("INSERT INTO messages (session_id, role, content, timestamp, active) "
                "VALUES ('s1', 'user', ?, 2.0, NULL)", (content,))
    con.commit()
    con.close()


def _active(path, content):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT active FROM messages WHERE content = ?", (content,)).fetchone()[0]
    finally:
        con.close()


def test_a_locked_null_active_heal_is_retried_after_the_short_backoff(tmp_path, reconciles, monkeypatch, caplog):
    """A memo-hit heal whose NULL-``active`` UPDATE meets a lock did not complete: the next open after
    ``HEAL_RETRY_S`` (far inside ``HEAL_INTERVAL_S``) must retry it and un-hide the turn."""
    path = tmp_path / "state.db"
    _legacy_nullable_active_store(path)
    SessionDB(db_path=path).close()  # full init: migrates and memoizes the store
    _hide_a_turn(path, "hidden while locked")
    locked = _lock_null_active_update(monkeypatch)
    _advance(monkeypatch, _memo().HEAL_INTERVAL_S + 1)
    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        SessionDB(db_path=path).close()  # memo hit: the throttled heal runs and meets the lock
    assert reconciles["n"] == 1
    assert _active(path, "hidden while locked") is None
    assert len(_heal_warnings(caplog)) == 1, "a locked NULL-active heal was taken as complete"
    locked["on"] = False  # the sibling's write lock is gone
    _advance(monkeypatch, _memo().HEAL_RETRY_S + 1)  # far inside a full interval
    SessionDB(db_path=path).close()
    assert reconciles["n"] == 1  # still a memo hit: the retry comes from the throttled heal
    assert _active(path, "hidden while locked") == 1


def test_a_full_init_keeps_the_delegate_heal_failure_when_the_null_active_heal_succeeds(
        tmp_path, reconciles, monkeypatch, caplog):
    """In the full init the NULL-``active`` heal runs after the delegate-marker heal: its success must not
    overwrite that heal's failure, or the store is recorded healed and the marker waits a full interval."""
    path = tmp_path / "state.db"
    _polluted_store_with_an_unindexed_message(path)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE unrelated_ddl (x)")  # a new PRAGMA schema_version: the next open is a full init
    con.commit()
    con.close()
    real_index = hermes_state_schema.SessionSchemaMixin._index_healed_rows_for_trigram
    null_active = {"n": 0, "ok": []}
    real_null = hermes_state_schema.SessionSchemaMixin._heal_null_active_rows

    def counted(self, cursor):
        null_active["n"] += 1
        ok = real_null(self, cursor)
        null_active["ok"].append(ok)
        return ok

    def locked(self, cursor, ids):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(hermes_state_schema.SessionSchemaMixin, "_heal_null_active_rows", counted)
    monkeypatch.setattr(hermes_state_schema.SessionSchemaMixin, "_index_healed_rows_for_trigram", locked)
    # Fresh process: no earlier open claimed the interval, so only the full init's own result decides it.
    _memo().clear()
    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        SessionDB(db_path=path).close()
    assert reconciles["n"] == 2  # the fixture's open, then this full init
    assert null_active["ok"][-1] is True  # the NULL-active heal after it did succeed
    assert len(_heal_warnings(caplog)) == 1, "the delegate heal's failure was dropped by the full init"
    monkeypatch.setattr(hermes_state_schema.SessionSchemaMixin, "_index_healed_rows_for_trigram", real_index)
    _advance(monkeypatch, _memo().HEAL_RETRY_S + 1)  # far inside a full interval
    SessionDB(db_path=path).close()
    assert reconciles["n"] == 2  # a memo hit
    con = sqlite3.connect(path)
    try:
        assert _marker(con) is None
    finally:
        con.close()


def test_a_heal_that_succeeds_ends_the_failure_streak_so_the_next_failure_warns_again(
        tmp_path, reconciles, monkeypatch, caplog):
    """fail (warning) -> succeed -> fail must warn twice: a success between two failures starts a new streak."""
    path = tmp_path / "state.db"
    _legacy_nullable_active_store(path)
    SessionDB(db_path=path).close()  # full init: memoized and healed
    memo = _memo()
    start = memo.time.monotonic()
    clock = {"t": start}
    monkeypatch.setattr(memo.time, "monotonic", lambda: clock["t"])
    locked = _lock_null_active_update(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="hermes_state"):
        _hide_a_turn(path, "first streak")
        clock["t"] = start + memo.HEAL_INTERVAL_S + 1
        SessionDB(db_path=path).close()  # fails: warning
        assert len(_heal_warnings(caplog)) == 1
        locked["on"] = False
        clock["t"] += memo.HEAL_RETRY_S + 1
        SessionDB(db_path=path).close()  # the retry succeeds
        assert _active(path, "first streak") == 1
        locked["on"] = True
        _hide_a_turn(path, "second streak")
        clock["t"] += memo.HEAL_INTERVAL_S + 1
        SessionDB(db_path=path).close()  # a new failure after a success: warning again, not debug
    assert reconciles["n"] == 1
    assert _active(path, "second streak") is None
    assert len(_heal_warnings(caplog)) == 2, "a success did not end the failure streak"
