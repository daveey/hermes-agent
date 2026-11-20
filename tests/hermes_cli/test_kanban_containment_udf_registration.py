"""Containment UDF registration must not reset active SQLite statements."""
import gc
import sqlite3
import weakref

import pytest

from hermes_cli import kanban_containment_guard as guard
from hermes_cli import kanban_containment_replica as replica
from tests.hermes_cli import test_kanban_containment_replica as fixtures

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


@pytest.mark.parametrize("operation", ["install", "remember", "guard", "snapshot"])
def test_registration_preserves_active_cursor(synced_conn, operation):
    conn = synced_conn
    target, _, stop, _, hub = fixtures.scenario(conn)
    replica.install(conn)
    cursor = conn.execute("SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3")
    assert cursor.fetchone()[0] == 1
    try:
        if operation == "install":
            replica.install(conn)
        elif operation == "remember":
            replica.remember(conn, hub.events[stop["event_uid"]], board="fixture", task=target, uid=stop["event_uid"])
        elif operation == "guard":
            guard.register(conn)
        else:
            # Snapshot registration is an intentional replacement, but must not
            # replace SQLite's callable while the connection has active work.
            guard.inert_snapshot(conn)
            guard.inert_snapshot(conn)
        assert [row[0] for row in cursor] == [2, 3]
    finally:
        cursor.close()


def test_certify_page_with_stop_and_release_preserves_active_cursor(synced_conn):
    conn = synced_conn
    target, child, stop, release, hub = fixtures.scenario(conn)
    changes = [fixtures.event(stop, hub, 1), fixtures.event(release, hub, 2)]
    cursor = conn.execute("SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3")
    assert cursor.fetchone()[0] == 1
    try:
        replica.certify_page(conn, hub, "fixture", changes)
        assert [row[0] for row in cursor] == [2, 3]
    finally:
        cursor.close()
    assert {row[0] for row in conn.execute("SELECT uid FROM containment_canonical_receipts")} == {
        stop["event_uid"], release["event_uid"]}
    fixtures.apply(conn, hub, changes)
    assert not fixtures.connections.held(conn, target)
    assert fixtures.kb.get_task(conn, child).status == "todo"


def test_guard_re_registration_updates_verifier(synced_conn, monkeypatch):
    conn = synced_conn
    monkeypatch.setattr(guard, "pending", lambda conn, task, verify_release: verify_release({}, {}))
    guard.register(conn, verify_release=lambda r, s: False)
    assert conn.execute("SELECT kanban_containment_held('fixture')").fetchone()[0] == 0
    cursor = conn.execute("SELECT 1 UNION ALL SELECT 2")
    try:
        guard.register(conn, verify_release=lambda r, s: True)
        assert conn.execute("SELECT kanban_containment_held('fixture')").fetchone()[0] == 1
        assert len(cursor.fetchall()) == 2
    finally:
        cursor.close()


def test_plain_connections_register_independently():
    with sqlite3.connect(":memory:") as first, sqlite3.connect(":memory:") as second:
        replica.register(first)
        replica.register(second)
        first.execute("BEGIN")
        token = replica._CACHE.set(id(first))
        try:
            assert first.execute("SELECT kanban_containment_cache_write()").fetchone()[0] == 1
            assert second.execute("SELECT kanban_containment_cache_write()").fetchone()[0] == 0
        finally:
            replica._CACHE.reset(token)
    first.close()
    second.close()


def test_close_releases_registration_bookkeeping():
    from hermes_cli import kanban_containment_functions as functions
    conn = sqlite3.connect(":memory:")
    replica.register(conn)
    key = (id(conn), "kanban_containment_cache_write", 0)
    callback = weakref.ref(functions._FUNCTIONS[key])
    conn.close()
    del conn
    gc.collect()
    assert callback() is None
    assert key not in functions._FUNCTIONS


def test_snapshot_callback_can_be_restored_without_sql_replacement():
    conn = sqlite3.connect(":memory:")
    try:
        replica.register(conn)
        conn.execute("BEGIN")
        token = replica._CACHE.set(id(conn))
        cursor = conn.execute("SELECT 1 UNION ALL SELECT 2")
        try:
            assert conn.execute("SELECT kanban_containment_cache_write()").fetchone()[0] == 1
            guard.inert_snapshot(conn)
            assert conn.execute("SELECT kanban_containment_cache_write()").fetchone()[0] == 0
            replica.register(conn)
            assert conn.execute("SELECT kanban_containment_cache_write()").fetchone()[0] == 1
            assert len(cursor.fetchall()) == 2
        finally:
            cursor.close()
            replica._CACHE.reset(token)
    finally:
        conn.close()
