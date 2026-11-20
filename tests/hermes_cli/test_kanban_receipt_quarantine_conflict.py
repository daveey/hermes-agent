"""Only a deterministic evidence conflict quarantines; every other refusal refuses the page (review r3, item 1)."""
from __future__ import annotations

import copy

import pytest

from hermes_cli import kanban_containment_connections as connections
from hermes_cli import kanban_containment_replica as replica
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_quarantine as quarantine
from hermes_cli.kanban_containment_lifecycle import ContainmentConflict, ContainmentRefused
from tests.hermes_cli import kanban_quarantine_support as support
from tests.hermes_cli import test_kanban_containment_replica as fixtures

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


def _feed(monkeypatch, changes):
    page = {"changes": changes, "next": changes[-1].seq, "more": False}
    monkeypatch.setattr(ksc.acw, "changes_page", lambda *args: page)
    monkeypatch.setattr(ksc, "adapt", lambda change: change)


@pytest.mark.parametrize("state", ["deleted", "unavailable"])
def test_non_live_evidence_refuses_the_page_without_quarantine_then_applies(synced_conn, monkeypatch, state):
    conn = synced_conn
    target, _, stop, _, hub = fixtures.scenario(conn)
    uid = stop["event_uid"]
    marker = sync.RemoteChange(1, "tasks", target, "upsert", {"status": "triage", "containment_episode": uid},
                               100, "", "home")
    _feed(monkeypatch, [marker, fixtures.event(stop, hub, 2)])
    before = sync.sync_state(conn)["cursor"]
    hub.events[uid]["state"] = state
    with pytest.raises(ContainmentRefused) as caught:
        ksc.pull(conn, hub, "fixture", observers=False)
    assert not isinstance(caught.value, ContainmentConflict)
    assert sync.sync_state(conn)["cursor"] == before
    assert support.count(conn, "containment_sync_quarantine") == 0
    assert not quarantine.tasks(conn)
    hub.events[uid]["state"] = "live"  # the transient cause is gone: the same page applies whole
    ksc.pull(conn, hub, "fixture", observers=False)
    assert sync.sync_state(conn)["cursor"] == 2
    assert conn.execute("SELECT 1 FROM task_events WHERE uid=?", (uid,)).fetchone() is not None
    assert kb.get_task(conn, target).containment_episode == uid
    assert connections.held(conn, target)
    assert kb.claim_task(conn, target) is None


def test_conflicts_are_typed_at_exactly_the_two_deterministic_sites(synced_conn):
    conn = synced_conn
    target, _, stop, _, hub = fixtures.scenario(conn)
    uid = stop["event_uid"]
    replica.remember(conn, hub.events[uid], board="fixture", task=target, uid=uid)
    changed = copy.deepcopy(hub.events[uid])
    changed["row"]["created_at"] += 1
    with pytest.raises(ContainmentConflict) as identity:
        replica.remember(conn, changed, board="fixture", task=target, uid=uid)
    assert (identity.value.kind, identity.value.uid) == ("identity_changed", uid)
    origin = fixtures.event(stop, hub, 1)
    origin = sync.RemoteChange(1, origin.table, origin.uid, origin.op, origin.fields, 1, "", "server")
    with pytest.raises(ContainmentConflict) as differs:
        replica.certify_page(conn, hub, "fixture", [origin])
    assert differs.value.kind == "event_differs"
    hub.events[uid]["state"] = "deleted"
    with pytest.raises(ContainmentRefused) as unproven:
        replica.certify_page(conn, hub, "fixture", [fixtures.event(stop, hub, 1)])
    assert not isinstance(unproven.value, ContainmentConflict)


def test_event_differs_quarantines_only_that_task(synced_conn):
    conn = synced_conn
    target, child, stop, _, hub = fixtures.scenario(conn)
    other = kb.create_task(conn, title="Unrelated", assignee="default")
    event = fixtures.event(stop, hub, 1)
    rehomed = sync.RemoteChange(1, event.table, event.uid, event.op, event.fields, 1, "", "server")
    healthy = sync.RemoteChange(2, "tasks", other, "upsert", {"title": "Unrelated synced"}, 200, "", "worker")
    assert quarantine.certified_page(conn, hub, "fixture", [rehomed, healthy]) == [healthy]
    [entry] = quarantine.entries(conn)
    assert entry["receipt"] == stop["event_uid"] and entry["kind"] == "event_differs"
    assert quarantine.held(conn, target) and quarantine.held(conn, child) and not quarantine.held(conn, other)
