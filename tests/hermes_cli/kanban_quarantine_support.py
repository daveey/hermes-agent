"""Shared fixtures for the receipt-quarantine tests (fixture Board doubles only, never a live store)."""
from __future__ import annotations

import copy
from types import SimpleNamespace

from hermes_cli import kanban_containment_replica as replica
from hermes_cli import kanban_sync_quarantine as quarantine
from tests.hermes_cli import test_kanban_containment_replica as fixtures


def conflict(conn, *, both: bool = False, before=None) -> SimpleNamespace:
    """Quarantine the stop receipt the way a Board re-stamp does (its certified ``created_at`` changes after this
    host remembered it). ``both`` also delivers the release event, whose certification re-checks that stop receipt,
    so a second receipt names the same task."""
    target, child, stop, release, hub = fixtures.scenario(conn)
    extra = before(target, child) if before else None
    uid = stop["event_uid"]
    replica.remember(conn, hub.events[uid], board="fixture", task=target, uid=uid)
    original = copy.deepcopy(hub.events[uid])
    hub.events[uid]["row"]["created_at"] += 1
    page = [fixtures.event(stop, hub, 1)]
    if both:
        page.append(fixtures.event(release, hub, 2))
    assert quarantine.certified_page(conn, hub, "fixture", page) == []
    return SimpleNamespace(target=target, child=child, stop=stop, release=release, hub=hub, original=original,
                           uid=uid, release_uid=release["event_uid"], extra=extra)


def count(conn, table: str) -> int:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return 0
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
