"""Authenticated closure projection using the canonical wire harness, no live writes."""
import copy
import json

import pytest

from hermes_cli import kanban_containment_connections as connections
from hermes_cli import kanban_containment_replica as replica
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import test_kanban_containment_replica as fixtures

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


def scenario(conn):
    target, child, stop, _, hub = fixtures.scenario(conn)
    episode = stop["event_uid"]
    payload = {"version": 1, "episode": episode,
               "containment_sha256": hub.events[episode]["containment_v1"]["containment_sha256"],
               "reason": "authority_archived", "authority": stop["payload"]["policy"]["authority"],
               "authority_status": "archived", "terminal_status": "archived"}
    uid = sync.uuid7()
    proof = copy.deepcopy(hub.events[episode])
    proof.update(uid=uid)
    proof["row"].update(uid=uid, kind="containment_closed", payload=json.dumps(payload))
    proof["containment_v1"]["request"] = sync.uuid7()
    hub.events[uid] = proof
    return target, child, stop, {"event_uid": uid}, hub


@pytest.mark.parametrize("order", ["task-first", "closure-first", "closure-before-stop"])
def test_authenticated_closure_keeps_terminal_archive_held(synced_conn, order):
    conn = synced_conn
    target, child, stop, close, hub = scenario(conn)
    marker = sync.RemoteChange(1, "tasks", target, "upsert",
                              {"status": "archived", "containment_episode": stop["event_uid"]}, 100, "", "home")
    fixtures.apply(conn, hub, [marker])
    assert kb.get_task(conn, target).status == "triage"
    if order != "closure-before-stop":
        fixtures.apply(conn, hub, [fixtures.event(stop, hub, 2)])
    fixtures.apply(conn, hub, [fixtures.event(close, hub, 3)])
    if order == "closure-before-stop":
        assert kb.get_task(conn, target).status == "triage"
        fixtures.apply(conn, hub, [fixtures.event(stop, hub, 4)])
    if order == "closure-first":
        fixtures.apply(conn, hub, [marker])
    assert kb.get_task(conn, target).status == "archived"
    assert connections.held(conn, target)
    assert kb.claim_task(conn, target) is None
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "todo"
    assert sync.outbox(conn) == []


@pytest.mark.parametrize("fault", ["attestation", "authority", "hash", "status", "reason"])
def test_invalid_closure_never_archives_replica(synced_conn, fault):
    from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
    conn = synced_conn
    target, _, stop, close, hub = scenario(conn)
    fixtures.apply(conn, hub, [fixtures.event(stop, hub, 1)])
    row = hub.events[close["event_uid"]]
    if fault == "attestation":
        row["containment_v1"] = None
    else:
        data = json.loads(row["row"]["payload"])
        key = {"authority": "authority", "hash": "containment_sha256", "status": "terminal_status", "reason": "reason"}[fault]
        data[key] = "forged"
        row["row"]["payload"] = json.dumps(data)
    with pytest.raises(ContainmentRefused):
        fixtures.apply(conn, hub, [fixtures.event(close, hub, 2)])
    assert kb.get_task(conn, target).status == "triage"


def test_authenticated_release_cannot_resurrect_closed_episode(synced_conn):
    conn = synced_conn
    target, _, stop, close, hub = scenario(conn)
    fixtures.apply(conn, hub, [fixtures.event(stop, hub, 1), fixtures.event(close, hub, 2)])
    release = next(p for p in hub.events.values() if p["row"]["kind"] == "containment_released")
    fixtures.apply(conn, hub, [fixtures.event({"event_uid": release["uid"]}, hub, 3)])
    assert connections.held(conn, target)
    assert kb.get_task(conn, target).status == "archived"


def test_cleared_marker_cannot_erase_terminal_episode(synced_conn):
    conn = synced_conn
    target, _, stop, close, hub = scenario(conn)
    fixtures.apply(conn, hub, [fixtures.event(stop, hub, 1), fixtures.event(close, hub, 2)])
    cleared = sync.RemoteChange(3, "tasks", target, "upsert",
                               {"status": "ready", "containment_episode": None}, 200, "", "home")
    fixtures.apply(conn, hub, [cleared])
    assert kb.get_task(conn, target).status == "archived"
    assert kb.get_task(conn, target).containment_episode == stop["event_uid"]
