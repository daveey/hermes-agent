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
