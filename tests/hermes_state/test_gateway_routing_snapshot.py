"""Routing polling must not rescan unchanged JSON for every notice candidate."""
import json
from datetime import UTC, datetime

import pytest

from hermes_state import SessionDB
from hermes_state_outcome import _gateway_turn_live


@pytest.fixture
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    yield store
    store.close()


def route(db, sid="chat", token: str | None = "tok"):
    db.save_gateway_routing_entry("route", json.dumps({
        "session_id": sid, "active_turn_token": token,
        "active_turn_started_at": datetime.now(UTC).isoformat(),
    }))


def trace(db):
    statements = []
    db._read_retrying_ioerr(lambda conn: conn.set_trace_callback(statements.append))
    return statements


def scans(statements):
    return [sql for sql in statements if "select" in sql.lower() and "gateway_routing" in sql.lower()]


def test_unchanged_candidates_do_not_rescan_routing(db, monkeypatch):
    monkeypatch.setattr("hermes_state_outcome._gateway_running", lambda home: True)
    route(db)
    statements = trace(db)
    assert _gateway_turn_live(db, "chat")
    assert len(scans(statements)) == 1
    statements.clear()
    for _ in range(10):
        assert _gateway_turn_live(db, "chat")
        assert not _gateway_turn_live(db, "other")
    assert scans(statements) == []


def test_committed_write_visible_next_call(db, monkeypatch):
    monkeypatch.setattr("hermes_state_outcome._gateway_running", lambda home: True)
    route(db)
    assert _gateway_turn_live(db, "chat")
    route(db, token=None)
    assert not _gateway_turn_live(db, "chat")
    route(db, sid="other")
    assert _gateway_turn_live(db, "other")
    assert not _gateway_turn_live(db, "chat")
    db.replace_gateway_routing_entries({})
    assert not _gateway_turn_live(db, "other")


def test_cached_rows_recheck_age_and_gateway(db, monkeypatch):
    import hermes_state_outcome as outcome

    alive = [True]
    monkeypatch.setattr(outcome, "_gateway_running", lambda home: alive[0])
    route(db)
    statements = trace(db)
    assert _gateway_turn_live(db, "chat")
    statements.clear()
    alive[0] = False
    assert not _gateway_turn_live(db, "chat")
    alive[0] = True
    assert _gateway_turn_live(db, "chat")
    now = outcome.time.time()
    monkeypatch.setattr(outcome.time, "time", lambda: now + outcome._GATEWAY_TURN_MAX_AGE_S + 1)
    assert not _gateway_turn_live(db, "chat")
    assert scans(statements) == []


def test_writer_fallback_and_rollback_do_not_reuse_uncommitted_rows(db, monkeypatch):
    monkeypatch.setattr(db, "_checkout_read_conn", lambda: None)
    monkeypatch.setattr("hermes_state_outcome._gateway_running", lambda home: True)
    route(db)
    assert _gateway_turn_live(db, "chat")
    route(db, token=None)
    assert not _gateway_turn_live(db, "chat")
    db._conn.execute("BEGIN")
    db._conn.execute("UPDATE gateway_routing SET entry_json = json_set(entry_json, '$.active_turn_token', 'tok')")
    assert _gateway_turn_live(db, "chat")
    db._conn.rollback()
    assert not _gateway_turn_live(db, "chat")
    db._conn.execute("BEGIN")
    db._conn.execute("UPDATE gateway_routing SET entry_json = json_set(entry_json, '$.active_turn_token', 'tok')")
    db._conn.rollback()
    assert not _gateway_turn_live(db, "chat")


def test_homes_and_reopened_connections_are_isolated(db, tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_state_outcome._gateway_running", lambda home: True)
    route(db)
    assert _gateway_turn_live(db, "chat")
    other_home = tmp_path / "other"
    other_home.mkdir()
    other = SessionDB(other_home / "state.db")
    try:
        route(other, sid="different")
        assert not _gateway_turn_live(other, "chat")
        assert _gateway_turn_live(other, "different")
        assert not _gateway_turn_live(db, "different")
        db.close()
        statements = trace(db)
        assert _gateway_turn_live(db, "chat")
        assert len(scans(statements)) == 1
    finally:
        other.close()


def test_pool_connections_do_not_share_data_versions(db, monkeypatch):
    monkeypatch.setattr("hermes_state_outcome._gateway_running", lambda home: True)
    route(db)
    assert _gateway_turn_live(db, "chat")
    with db._read_ctx() as first:
        # Holding the warm reader forces a distinct connection checkout.
        statements = trace(db)
        assert _gateway_turn_live(db, "chat")
        assert len(scans(statements)) == 1
        route(db, token=None)
        assert not _gateway_turn_live(db, "chat")
        assert first is not db._conn
    assert not _gateway_turn_live(db, "chat")


def test_commit_during_scan_is_not_published_under_stale_revision(db, monkeypatch):
    import hermes_state_routing_snapshot as snapshot

    monkeypatch.setattr("hermes_state_outcome._gateway_running", lambda home: True)
    route(db)
    original = snapshot._scan
    def scan_then_commit(conn):
        grouped = original(conn)
        route(db, token=None)
        return grouped
    monkeypatch.setattr(snapshot, "_scan", scan_then_commit)
    assert _gateway_turn_live(db, "chat")
    monkeypatch.setattr(snapshot, "_scan", original)
    statements = trace(db)
    assert not _gateway_turn_live(db, "chat")
    assert len(scans(statements)) == 1


def test_cache_evicts_old_connections(db, tmp_path, monkeypatch):
    import hermes_state_routing_snapshot as snapshot

    monkeypatch.setattr("hermes_state_outcome._gateway_running", lambda home: True)
    monkeypatch.setattr(snapshot, "_MAX_CONNECTIONS", 2)
    route(db)
    assert _gateway_turn_live(db, "chat")
    stores = [SessionDB(tmp_path / f"state-{i}.db") for i in range(2)]
    try:
        for store in stores:
            assert not _gateway_turn_live(store, "chat")
        statements = trace(db)
        assert _gateway_turn_live(db, "chat")
        assert len(scans(statements)) == 1
    finally:
        for store in stores:
            store.close()
