"""Slow writer provenance must not change commit/rollback semantics."""
import logging
import sqlite3

import pytest

import hermes_state
from hermes_state import SessionDB
import hermes_state_write_diagnostics as diagnostics


@pytest.fixture
def db(tmp_path, monkeypatch):
    store = SessionDB(tmp_path / "state.db")
    store._conn.execute("CREATE TABLE diagnostic_fixture (value TEXT)")
    monkeypatch.setattr(hermes_state, "_WRITE_HOLD_WARN_S", 0)
    yield store
    store.close()


def semantic_operation(db):
    db._write_sql("INSERT INTO diagnostic_fixture VALUES ('literal-secret')")


def test_wrapper_provenance_no_values(db, caplog):
    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        semantic_operation(db)
    assert "semantic_operation" in caplog.text
    assert "stage=committed" in caplog.text
    assert "literal-secret" not in caplog.text
    assert "INSERT" not in caplog.text
    assert db._conn.execute("SELECT count(*) FROM diagnostic_fixture").fetchone()[0] == 1


def test_failed_callback_is_attributed_and_rolled_back_once(db, caplog):
    calls = []

    def failed_operation(conn):
        calls.append(1)
        conn.execute("INSERT INTO diagnostic_fixture VALUES (?)", ("parameter-secret",))
        raise ValueError("exception-secret")

    with caplog.at_level(logging.WARNING, logger="hermes_state"), pytest.raises(ValueError):
        db._execute_write(failed_operation)
    assert "stage=callback" in caplog.text
    assert "test_failed_callback_is_attributed" in caplog.text
    assert "parameter-secret" not in caplog.text
    assert "exception-secret" not in caplog.text
    assert calls == [1]
    assert db._conn.execute("SELECT count(*) FROM diagnostic_fixture").fetchone()[0] == 0


def test_logging_failure_cannot_turn_commit_into_error(db, monkeypatch):
    def broken_handler(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(diagnostics.logger, "warning", broken_handler)
    semantic_operation(db)
    assert db._conn.execute("SELECT count(*) FROM diagnostic_fixture").fetchone()[0] == 1
