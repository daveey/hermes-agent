"""Pickup copies a released card's containment history from the replica with the real containment triggers.

The replica file is filled through ``certify_page`` (it holds the authenticated receipts); the live file gets the
copy through ``worker_pickup`` -> ``import_cards`` -> ``apply_remote_changes`` with no Hub. One card whose
receipts are missing must not abort the import of the others, and the guard itself stays closed.
"""
from __future__ import annotations

import logging
import sqlite3

import pytest

from hermes_cli import kanban_containment_connections as connections
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_containment_replica as base
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli import test_kanban_sync_capture as capture

kanban_home = capture.kanban_home


@pytest.fixture
def pair(kanban_home, tmp_path):
    support.write_sync_config(kanban_home, True)
    with kbc.connect(tmp_path / "replica.db") as replica, kbc.connect(kanban_home / "kanban.db") as live:
        client.ensure_identity(live, "fixture", "worker")
        sync.set_mode(live, "primary")
        previous = sync.set_lease_client(support.FakeLeaseClient())
        try:
            yield replica, live
        finally:
            sync.set_lease_client(previous)


def released_card(replica):
    """A card the replica holds as released (stop + release receipts cached), plus its Hub double."""
    target, _child, stop, release, hub = base.scenario(replica)
    marker = sync.RemoteChange(1, "tasks", target, "upsert",
                               {"status": "triage", "containment_episode": stop["event_uid"]}, 100, "", "home")
    base.apply(replica, hub, [marker])
    base.apply(replica, hub, [base.event(stop, hub, 2)])
    cleared = sync.RemoteChange(3, "tasks", target, "upsert", {"status": "ready", "containment_episode": None},
                                101, "", "home")
    base.apply(replica, hub, [cleared])
    base.apply(replica, hub, [base.event(release, hub, 4)])
    assert kb.get_task(replica, target).status == "ready"
    return target, stop, release


def plain_card(replica, title):
    return kb.create_task(replica, title=title, assignee="default")


def test_released_card_imports_and_can_be_claimed(pair):
    replica, live = pair
    target, _stop, _release = released_card(replica)
    other = plain_card(replica, "Plain ready card")
    assert sorted(wn.import_cards(live, replica, [target, other])) == sorted([target, other])
    kinds = {r[0] for r in live.execute("SELECT kind FROM task_events WHERE task_id=?", (target,))}
    assert {"contained", "containment_released"} <= kinds
    assert not connections.held(live, target)
    assert kb.claim_task(live, target) is not None


def test_uncertifiable_card_is_skipped_visibly_and_others_import(pair, caplog, monkeypatch):
    """Through ``worker_pickup``: the refused card is recorded and logged; every other card is imported."""
    replica, live = pair
    bad, _stop, release = released_card(replica)
    good, _s, _r = released_card(replica)
    other = plain_card(replica, "Plain ready card")
    # The replica lost the release receipt of `bad` (and nothing else).
    replica.execute("DROP TRIGGER containment_cache_delete")
    replica.execute("DELETE FROM containment_canonical_receipts WHERE uid=?", (release["event_uid"],))
    picked = [bad, good, other]
    monkeypatch.setattr(wn, "pickup_mode", lambda: "all")
    monkeypatch.setattr(wn, "open_replica", lambda conn: _Keep(replica))
    monkeypatch.setattr(wn, "_evaluate", lambda *a, **k: (list(picked), {}))
    from hermes_cli import kanban_hub_admission_pickup as admission
    monkeypatch.setattr(admission, "screen", lambda conn, rep, own, ids: (list(ids), {}, {}))
    with caplog.at_level(logging.WARNING):
        sync.worker_pickup(live, 10, False, lambda: None)
    assert not [r for r in caplog.records if "pickup failed" in r.getMessage()]
    assert {r[0] for r in live.execute("SELECT id FROM tasks")} >= {good, other}
    assert live.execute("SELECT 1 FROM tasks WHERE id=?", (bad,)).fetchone() is None
    assert live.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind LIKE 'contain%'",
                        (bad,)).fetchone()[0] == 0
    assert any(bad in r.getMessage() and "task_events" in r.getMessage() for r in caplog.records)
    skip = live.execute("SELECT reason, detail FROM kanban_pickup_skips WHERE task_id=?", (bad,)).fetchone()
    assert skip[0] == "other" and "containment receipts not certified" in skip[1]
    assert kb.claim_task(live, good) is not None


class _Keep:
    """The replica handle pickup closes when done: keep the fixture's connection open."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_guard_still_refuses_uncertified_history_and_edges(pair):
    replica, live = pair
    target, _stop, _release = released_card(replica)
    wn.import_cards(live, replica, [target])
    fresh = sync.uuid7()
    live.execute("UPDATE kanban_sync_state SET applying=1")
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        live.execute("INSERT INTO task_events(uid,task_id,kind,payload,created_at) VALUES (?,?,?,?,1)",
                     (fresh, target, "contained", "{}"))
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        live.execute("INSERT INTO task_events(uid,task_id,kind,payload,created_at) VALUES (?,?,?,?,1)",
                     (fresh, target, "containment_released", "{}"))
