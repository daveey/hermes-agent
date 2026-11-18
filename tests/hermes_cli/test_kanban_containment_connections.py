"""Connection policy uses durable sync identity, not ambient configuration."""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_containment_connections as connections


@pytest.mark.parametrize("sync_state", ["absent", "empty", "enabled"])
def test_verifier_selects_durable_sync_boundary(monkeypatch, sync_state):
    from hermes_cli import kanban_containment_lifecycle as local
    from hermes_cli import kanban_containment_replica as remote

    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE containment_attestations (uid TEXT)")
        if sync_state != "absent":
            conn.execute("CREATE TABLE kanban_sync_state (board_id TEXT)")
        if sync_state == "enabled":
            conn.execute("INSERT INTO kanban_sync_state VALUES ('fixture')")
        calls = []
        receipt, stop = object(), object()

        def checked(kind):
            def check(c, r, s):
                assert c is conn and r is receipt and s is stop
                calls.append(kind)
                return True
            return check

        monkeypatch.setattr(local, "verified_release", checked("local"))
        monkeypatch.setattr(remote, "verified", checked("canonical"))
        assert connections.verifier(conn, receipt, stop) is True
        assert calls == ["canonical" if sync_state == "enabled" else "local"]


def test_dispatch_filter_and_refusal_keep_ordinary_rows(monkeypatch):
    # The policy here is the caller's selection; real held-history integration is
    # separately exercised by maintenance/paging tests. Keep both row shapes.
    held = {"id": "t_0123456789abcdef"}
    ordinary = {"id": "t_1123456789abcdef"}
    conn = sqlite3.connect(":memory:")
    calls = []

    def check(c, task):
        assert c is conn
        calls.append(task)
        return task == held["id"]

    monkeypatch.setattr(connections, "held", check)
    assert connections.drop_held(conn, [held, ordinary]) == [ordinary]
    assert connections.refusal(conn, held["id"]) == "containment_pending: scoped resume-contained required"
    assert connections.refusal(conn, ordinary["id"]) is None
    assert calls == [held["id"], ordinary["id"], held["id"], ordinary["id"]]
    conn.close()
