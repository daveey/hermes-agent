"""Wholesale FTS free gates: fts_free_gate_refusals.

Split by lifecycle path to keep each real-corruption fixture group within the
300-second per-file budget on shared macOS utility-QoS runners.
"""


import sqlite3


import pytest

import hermes_state_fts_online

import hermes_state_schema_memo

from hermes_state import SessionDB


from hermes_state_common import FTS_STALE_KEY

from tests.hermes_state.test_fts_online_recovery import _meta, _seed


from tests.hermes_state import fts_free_gate_helpers as fixtures
from tests.hermes_state.fts_free_gate_helpers import (
    _behind_v30,
    _break,
    _stale_source_view,
)

def test_trash_verification_detaches_under_the_session_lock(store, monkeypatch):
    """The trash check plans outside self._lock; when its plan names a family, the detach must wait for the lock,
    never write inside another thread's open transaction on the shared connection."""
    import threading

    path, _before = store
    _break(path, "messages_fts_trigram_data")
    db = SessionDB(db_path=path)
    try:
        db._fts_stale = False
        plan = hermes_state_fts_online.plan_online_recovery(path)
        assert plan["unlink"] == ["messages_fts_trigram"]
        monkeypatch.setattr(hermes_state_fts_online, "plan_online_recovery", lambda _p: plan)
        held, release = threading.Event(), threading.Event()

        def writer():
            with db._lock:
                db._conn.execute("BEGIN IMMEDIATE")
                held.set()
                release.wait(5)
                db._conn.execute("ROLLBACK")

        t = threading.Thread(target=writer)
        t.start()
        held.wait(5)
        done = []
        v = threading.Thread(target=lambda: done.append(db._fts_verify_trash(["fts_v22_trash_x"])))
        v.start()
        v.join(0.5)
        assert v.is_alive(), "the detach did not wait for the session lock"
        release.set()
        t.join()
        v.join(15)
        assert done == [set()]
        assert _meta(path, FTS_STALE_KEY) == "1"  # committed on its own, not rolled back with the writer
    finally:
        db.close()

# Register shared fixtures without importing names shadowed by test arguments.
store = fixtures.store


def test_a_remembered_refusal_logs_at_debug_on_runtime_paths(store, monkeypatch, caplog):
    import logging

    path, _before = store
    db = SessionDB(db_path=path)
    try:
        hermes_state_fts_online.write_refusal_memo(path, "earlier damage")
        caplog.set_level(logging.DEBUG, logger="hermes_state")
        with pytest.raises(hermes_state_fts_online.FtsFreeRefused):
            db.optimize_fts()
        skipped = [r for r in caplog.records if "skipped" in r.getMessage()]
        assert skipped and all(r.levelno == logging.DEBUG for r in skipped)
    finally:
        hermes_state_fts_online.write_refusal_memo(path, None)
        db.close()


def test_a_transient_plan_failure_is_not_remembered(store, monkeypatch):
    path, _before = store
    db = SessionDB(db_path=path)
    try:
        import hermes_cli.sqlite_safe_read as safe_read

        real = safe_read.connect_tracked

        def busy(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(safe_read, "connect_tracked", busy)
        with pytest.raises(hermes_state_fts_online.FtsFreeRefused):
            db.optimize_fts()
        assert hermes_state_fts_online.read_refusal_memo(path) is None
        monkeypatch.setattr(safe_read, "connect_tracked", real)
        assert db.optimize_fts() == 2
    finally:
        db.close()


def test_optimize_returns_zero_when_the_schema_moved_since_the_plan(store, monkeypatch):
    path, _before = store
    db = SessionDB(db_path=path)
    try:
        real = hermes_state_fts_online.plan_online_recovery

        def moved(p):
            plan = real(p)
            plan["roots"] = {n: r + 1000 for n, r in plan["roots"].items()}
            return plan

        monkeypatch.setattr(hermes_state_fts_online, "plan_online_recovery", moved)
        assert db.optimize_fts() == 0
        assert db._fts_gate_outcome == "moved"
    finally:
        db.close()


def test_the_schema_memo_still_records_an_open(tmp_path):
    path = tmp_path / "state.db"
    _seed(path, sessions=1, per=4)
    hermes_state_schema_memo.clear()
    SessionDB(db_path=path).close()
    assert hermes_state_schema_memo._memo, "the per-thread plan state disabled the schema-init memo"


def test_a_remembered_refusal_replayed_at_open_stays_at_debug(store, caplog):
    import logging

    path, _before = store
    _stale_source_view(path, claim=False)
    _behind_v30(path)
    hermes_state_fts_online.write_refusal_memo(path, "earlier damage")
    caplog.set_level(logging.DEBUG, logger="hermes_state")
    db = SessionDB(db_path=path)
    try:
        skipped = [r for r in caplog.records if "skipped: earlier damage" in r.getMessage()]
        assert skipped and all(r.levelno == logging.DEBUG for r in skipped)
    finally:
        hermes_state_fts_online.write_refusal_memo(path, None)
        db.close()


