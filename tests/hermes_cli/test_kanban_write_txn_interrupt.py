"""Real signal cleanup regressions; no SQLite or cleanup substitutes."""
import signal
import sqlite3
import sys

import pytest

from hermes_cli import kanban_db_connect as kbc


@pytest.mark.parametrize("phase", ["body", "commit"])
@pytest.mark.parametrize("interrupt", [True, False])
def test_original_error_survives_rollback_interrupt(tmp_path, phase, interrupt):
    db = tmp_path / "boundary.db"
    a = sqlite3.connect(db, isolation_level=None, timeout=0)
    b = sqlite3.connect(db, isolation_level=None, timeout=0)
    a.execute("PRAGMA foreign_keys=ON")
    a.execute("CREATE TABLE parent(id PRIMARY KEY)")
    a.execute("CREATE TABLE child(pid REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
    original = ValueError("body failure")
    releases = []
    signals = []

    def done():
        try:
            b.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            releases.append(False)
        else:
            releases.append(True)
            b.execute("ROLLBACK")

    def trace(frame, event, arg):
        if interrupt and event == "call" and frame.f_code is kbc._TxnBoundary.rollback.__code__:
            sys.settrace(None)
            signals.append(True)
            signal.raise_signal(signal.SIGINT)
        return trace

    caught = None
    try:
        try:
            sys.settrace(trace)
            with kbc.write_txn(a, no_retry=True, boundary_done=done):
                a.execute("INSERT INTO child VALUES (99)")
                if phase == "body":
                    raise original
        except BaseException as exc:
            caught = exc
        finally:
            sys.settrace(None)
        assert signals == ([True] if interrupt else [])
        if phase == "body":
            assert caught is original
        else:
            assert isinstance(caught, sqlite3.IntegrityError)
            assert "FOREIGN KEY" in str(caught)
        assert all(releases), "callback released a live writer"
        if not interrupt:
            assert releases == [True]
        assert len(releases) <= 1
        b.execute("BEGIN IMMEDIATE")  # writer ownership really ended
        b.execute("ROLLBACK")
        try:
            assert not a.in_transaction
        except sqlite3.ProgrammingError:
            pass  # boundary-owned close also ends ownership
    finally:
        sys.settrace(None)
        a.close()
        b.close()
