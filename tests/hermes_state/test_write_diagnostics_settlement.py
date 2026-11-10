"""Failure injection at COMMIT: diagnostics cannot replay uncertain commits."""
import logging
import sqlite3

import pytest

import hermes_state
from hermes_state import SessionDB


@pytest.mark.parametrize("settled", [False, True])
def test_commit_failure_never_replays(tmp_path, monkeypatch, caplog, settled):
    db = SessionDB(tmp_path / "state.db")
    real = db._conn
    assert real is not None
    real.execute("CREATE TABLE diagnostic_fixture(value TEXT)")
    calls = []

    class CommitFailure:
        def __getattr__(self, name):
            return getattr(real, name)

        def commit(self):
            if settled:
                real.commit()
            raise sqlite3.OperationalError("disk I/O error secret")

    monkeypatch.setattr(db, "_conn", CommitFailure())
    monkeypatch.setattr(hermes_state, "_WRITE_HOLD_WARN_S", 0)

    def operation(conn):
        calls.append(1)
        conn.execute("INSERT INTO diagnostic_fixture VALUES ('secret')")

    try:
        with caplog.at_level(logging.WARNING, logger="hermes_state"), pytest.raises(sqlite3.OperationalError):
            db._execute_write(operation)
        assert calls == [1]
        assert "stage=commit" in caplog.text
        assert "secret" not in caplog.text
        assert real.execute("SELECT count(*) FROM diagnostic_fixture").fetchone()[0] == int(settled)
    finally:
        db._conn = real
        db.close()


def test_many_and_rowcount_are_attributed(tmp_path, monkeypatch, caplog):
    db = SessionDB(tmp_path / "state.db")
    assert db._conn is not None
    db._conn.execute("CREATE TABLE diagnostic_fixture(value TEXT)")
    monkeypatch.setattr(hermes_state, "_WRITE_HOLD_WARN_S", 0)
    try:
        with caplog.at_level(logging.WARNING, logger="hermes_state"):
            db._write_sql("INSERT INTO diagnostic_fixture VALUES (?)", [("secret",), ("secret",)], many=True)
            assert db._write_rowcount("DELETE FROM diagnostic_fixture") == 2
        assert "SessionDB._write_sql" in caplog.text
        assert "SessionDB._write_rowcount" in caplog.text
        assert "test_many_and_rowcount" in caplog.text
        assert "secret" not in caplog.text
    finally:
        db.close()
