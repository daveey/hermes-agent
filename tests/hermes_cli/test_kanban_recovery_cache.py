"""Recovery caches are bounded and isolated by authoritative endpoint identity."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_placement_recovery as recovery
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_placement_recovery as fixtures

conn = fixtures.conn


@pytest.fixture(autouse=True)
def isolated_caches(monkeypatch):
    monkeypatch.setattr(recovery, "_unsupported", {})
    monkeypatch.setattr(recovery, "_scan_cursor", {})
    monkeypatch.setattr(recovery.time, "monotonic", lambda: 100.0)


def test_backoff_shared_across_new_clients_but_not_url_board_gateway():
    hub = SimpleNamespace(ident=SimpleNamespace(url="https://one.invalid"))
    again = SimpleNamespace(ident=SimpleNamespace(url="https://one.invalid"))
    other = SimpleNamespace(ident=SimpleNamespace(url="https://two.invalid"))
    assert recovery.unavailable_endpoint(hub, "board", "gateway", client.HubError(404, "missing"), "promote")
    assert not recovery.endpoint_available(again, "board", "gateway")
    assert recovery.endpoint_available(other, "board", "gateway")
    assert recovery.endpoint_available(hub, "other", "gateway")
    assert recovery.endpoint_available(hub, "board", "other")


def test_reservation_errors_do_not_poison_recovery():
    hub = object()
    assert not recovery.unavailable_endpoint(hub, "b", "g", client.HubError(404, "missing"), None)
    assert recovery.endpoint_available(hub, "b", "g")


def test_backoff_expires_at_boundary(monkeypatch):
    hub = object()
    recovery.unavailable_endpoint(hub, "b", "g", client.HubError(404, "missing"), "promote")
    monkeypatch.setattr(recovery.time, "monotonic", lambda: 699.999)
    assert not recovery.endpoint_available(hub, "b", "g")
    monkeypatch.setattr(recovery.time, "monotonic", lambda: 700.0)
    assert recovery.endpoint_available(hub, "b", "g")


def test_backoff_has_hard_bound_and_keeps_latest():
    hub = object()
    for i in range(1025):
        recovery.unavailable_endpoint(hub, str(i), "g", client.HubError(404, "missing"), "promote")
    assert len(recovery._unsupported) == 1024
    assert recovery.endpoint_available(hub, "0", "g")
    assert not recovery.endpoint_available(hub, "1024", "g")


def test_scan_cursor_is_board_scoped_and_bounded(conn):
    ids = sorted(fixtures.make(conn) for _ in range(2))
    for i in range(1025):
        rows = recovery.select_rows(conn, str(i), "hub", "", 1)
        assert [r["id"] for r in rows] == ids[:1]
    assert len(recovery._scan_cursor) == 1024
    assert [r["id"] for r in recovery.select_rows(conn, "1024", "hub", "", 1)] == ids[1:]
    assert [r["id"] for r in recovery.select_rows(conn, "0", "hub", "", 1)] == ids[:1]


def test_empty_scan_does_not_allocate_cursor(conn):
    assert recovery.select_rows(conn, "b", "hub", "", 1) == []
    assert recovery._scan_cursor == {}


@pytest.mark.parametrize("dimension", ["database", "gateway"])
def test_scan_cursor_does_not_cross_storage_owner(conn, monkeypatch, dimension):
    from hermes_cli import kanban_db_connect as connect
    ids = sorted(fixtures.make(conn) for _ in range(2))
    monkeypatch.setattr(connect, "_main_db_file", lambda db: "one.db")
    assert [r["id"] for r in recovery.select_rows(conn, "b", "hub", "", 1)] == ids[:1]
    gateway = "hub"
    if dimension == "database":
        # Same contents from a different physical database must start its own cursor.
        monkeypatch.setattr(connect, "_main_db_file", lambda db: "two.db")
    else:
        gateway = "other"
        conn.execute("UPDATE tasks SET created_on=?", (gateway,))
        conn.execute("DELETE FROM kanban_sync_outbox")
        conn.commit()
    assert [r["id"] for r in recovery.select_rows(conn, "b", gateway, "", 1)] == ids[:1]


@pytest.mark.parametrize("state", ["pending", "rejected"])
def test_scan_excludes_unacknowledged_writes_before_spending_slot(conn, state):
    tid = fixtures.make(conn)
    conn.execute("UPDATE tasks SET title='pending edit' WHERE id=?", (tid,))
    conn.execute("UPDATE kanban_sync_outbox SET state=? WHERE table_name='tasks' AND row_uid=?", (state, tid))
    conn.commit()
    assert conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE row_uid=? AND state=?", (tid, state)).fetchone()
    assert recovery.select_rows(conn, "b", "hub", "", 1) == []


