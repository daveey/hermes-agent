"""A kanban notice that is already delivered must not hold the state.db write lock. [daveey]

hermes-hub 2026-10-29: 328 "write lock unavailable" warnings. py-spy of the holder (a desktop-ssh
``hermes serve``) caught the unloaded-notice sweep thread inside ``record_kanban_notice``'s writer txn
(hermes_state_messages.py:505): the notice-id dedupe query planned ``SCAN m`` over all 300k messages
(1.9 GB) while holding BEGIN IMMEDIATE, and the sweep re-offers every done card every 30 s.
"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard
import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_messages import _NOTICE_BY_ID_SQL
from hermes_state_sessions import _LINEAGE_CTE_SQL

META = {"notice_id": "n:1", "kanban_event_keys": ["default:1"]}


@pytest.fixture
def db(tmp_path):
    d = SessionDB(tmp_path / "state.db")
    d.create_session("chat", "desktop")
    yield d
    d.close()


def test_notice_dedupe_query_never_scans_messages(db):
    conn = db._conn
    plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + _LINEAGE_CTE_SQL + _NOTICE_BY_ID_SQL, ("chat", "chat", "x"))]
    assert not any(p.startswith("SCAN m") for p in plan), plan
    assert any("SEARCH m USING" in p for p in plan), plan


def test_already_delivered_notice_takes_no_write_lock(db, monkeypatch):
    rid, inserted = db.record_kanban_notice("chat", "hello", META)
    assert inserted

    def boom(*a, **k):
        raise AssertionError("duplicate notice opened a write txn")

    monkeypatch.setattr(db, "_execute_write", boom)
    assert db.record_kanban_notice("chat", "hello", META) == (rid, False)
    # dedupe by event key alone (a different notice_id) is also answered without the writer
    assert db.record_kanban_notice("chat", "x", {"notice_id": "n:2", "kanban_event_keys": ["default:1"]}) == (rid, False)


def test_duplicate_notice_while_another_process_holds_write_lock(db, tmp_path):
    rid, _ = db.record_kanban_notice("chat", "hello", META)
    other = sqlite3.connect(tmp_path / "state.db", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        assert db.record_kanban_notice("chat", "hello", META) == (rid, False)
    finally:
        other.execute("ROLLBACK")
        other.close()


def test_slow_write_txn_logs_its_callback_name(db, caplog, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "_WRITE_HOLD_WARN_S", 0.0)

    def my_slow_callback(conn):
        return conn.execute("SELECT 1").fetchone()[0]

    with caplog.at_level("WARNING", logger="hermes_state"):
        assert db._execute_write(my_slow_callback) == 1
    assert any("write lock held" in r.getMessage() and "my_slow_callback" in r.getMessage() for r in caplog.records)
