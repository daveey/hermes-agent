"""Real replica apply with authenticated-Hub doubles (not live Board transport)."""
from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from hermes_cli import kanban_containment_connections as connections
from hermes_cli import kanban_containment_replica as replica
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from hermes_cli.kanban_containment_state import fingerprint
from tests.hermes_cli import test_kanban_sync_capture as capture
from tests.hermes_cli import test_kanban_containment_canonical as wire

kanban_home = capture.kanban_home
synced_conn = capture.synced_conn


def scenario(conn):
    sync.set_identity(conn, "fixture", "worker")
    target = kb.create_task(conn, title="Fixture relay", assignee="default")
    child = kb.create_task(conn, title="Fixture waiter", assignee="default", parents=[target])
    op, stop, episode = wire.operation()
    op["task"] = target
    stop["target"]["task_id"] = target
    op["payload"] = stop
    hub = wire.Hub(op, stop, episode)
    hub.events[episode]["row"]["task_id"] = target
    hub.events[episode]["containment_v1"]["containment_sha256"] = fingerprint(stop)
    release, _, _ = wire.operation("resume_contained")
    release.update(task=target, payload={"version": 1, "episode": episode,
        "containment_sha256": fingerprint(stop), "resume_status": "ready",
        "evidence": {"kind": "authority", "authority": stop["policy"]["authority"], "run_uid": sync.uuid7()}})
    proof = wire.evidence(release, stop, episode)
    proof["row"]["task_id"] = target
    hub.events[release["event_uid"]] = proof
    for receipt in hub.events.values():
        receipt["row"]["created_at"] = 1
    capture._clear_outbox(conn)
    return target, child, op, release, hub


def event(op, hub, seq):
    row = hub.events[op["event_uid"]]["row"]
    return sync.RemoteChange(seq, "task_events", op["event_uid"], "upsert", dict(row), seq, "", "worker")


def apply(conn, hub, changes):
    replica.certify_page(conn, hub, "fixture", changes)
    return sync.apply_remote_changes(conn, changes, new_cursor=changes[-1].seq, board="fixture", observers=False)


@pytest.mark.parametrize("order", ["stop-task-first", "release-task-first", "release-event-first", "release-before-stop-history"])
def test_authenticated_pages_converge_without_ever_releasing_consumer(synced_conn, order):
    conn = synced_conn
    target, child, stop, release, hub = scenario(conn)
    links = [tuple(r) for r in conn.execute("SELECT parent_id,child_id FROM task_links")]
    marker = sync.RemoteChange(1, "tasks", target, "upsert", {"status": "triage", "containment_episode": stop["event_uid"]}, 100, "", "home")
    apply(conn, hub, [marker])
    assert connections.held(conn, target)
    assert kb.claim_task(conn, target) is None
    if order != "release-before-stop-history":
        apply(conn, hub, [event(stop, hub, 2)])
    cleared = sync.RemoteChange(3, "tasks", target, "upsert", {"status": "ready", "containment_episode": None}, 101, "", "home")
    if order in ("release-task-first", "release-before-stop-history"):
        apply(conn, hub, [cleared])
        assert kb.get_task(conn, target).status == "triage"
        assert connections.held(conn, target)
    apply(conn, hub, [event(release, hub, 4)])
    if order == "release-before-stop-history":
        assert connections.held(conn, target)
        assert kb.get_task(conn, target).status == "triage"
        apply(conn, hub, [event(stop, hub, 5)])
    if order in ("stop-task-first", "release-event-first"):
        apply(conn, hub, [cleared])
    assert not connections.held(conn, target)
    assert kb.get_task(conn, target).status == "ready"
    assert kb.get_task(conn, target).containment_episode is None
    assert kb.get_task(conn, child).status == "todo"
    assert [tuple(r) for r in conn.execute("SELECT parent_id,child_id FROM task_links")] == links
    assert sync.outbox(conn) == []
    # Restart registration uses the durable canonical cache, never local attestation.
    from hermes_cli import kanban_db_connect as kbc
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    with kbc.connect_closing(db_path=__import__("pathlib").Path(path)) as reopened:
        assert not connections.held(reopened, target)
        reopened.execute("UPDATE tasks SET status='todo' WHERE id=?", (target,))


def test_applying_flag_does_not_authorize_uncertified_history(synced_conn):
    conn = synced_conn
    target, _, stop, _, hub = scenario(conn)
    change = event(stop, hub, 1)
    replica.prepare_apply(conn, [change])
    conn.execute("UPDATE kanban_sync_state SET applying=1")
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        conn.execute("INSERT INTO task_events(uid,task_id,kind,payload,created_at) VALUES (?,?,?,?,1)",
                     (change.uid, target, "contained", change.fields["payload"]))
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        sync.apply_remote_changes(conn, [change], new_cursor=1, board="fixture", observers=False)
    assert not conn.in_transaction
    assert conn.execute("SELECT cursor FROM kanban_sync_state").fetchone()[0] == 0


@pytest.mark.parametrize("fault", ["payload", "origin", "board", "uid", "request"])
def test_canonical_page_mismatch_fails_closed(synced_conn, fault):
    conn = synced_conn
    target, _, stop, _, hub = scenario(conn)
    change = event(stop, hub, 1)
    proof = hub.events[stop["event_uid"]]
    if fault == "payload":
        proof["row"]["payload"] = "{}"
    elif fault == "origin":
        change = sync.RemoteChange(1, change.table, change.uid, change.op, change.fields, 1, "", "foreign")
    elif fault == "request":
        proof["containment_v1"]["request"] = "bad"
    else:
        proof[fault] = "wrong"
    with pytest.raises(ContainmentRefused):
        replica.certify_page(conn, hub, "fixture", [change])
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='contained'").fetchone()[0] == 0
    assert kb.get_task(conn, target).status == "ready"


def test_stale_lifecycle_outbox_is_removed_without_mutating_replay_bytes(synced_conn):
    conn = synced_conn
    target, _, stop, _, hub = scenario(conn)
    conn.execute("UPDATE tasks SET status='todo',priority=17 WHERE id=?", (target,))
    before = sync.outbox(conn)
    assert before
    ids = {op["op_id"] for op in before}
    marker = sync.RemoteChange(1, "tasks", target, "upsert", {"status": "ready", "containment_episode": stop["event_uid"]}, 100, "", "home")
    result = apply(conn, hub, [marker])
    assert kb.get_task(conn, target).status == "triage"
    assert result.hooks == []
    after = sync.outbox(conn)
    assert after
    assert ids.isdisjoint({op["op_id"] for op in after})
    assert all("status" not in op["fields"] for op in after)
    assert any(op["fields"].get("priority") == 17 for op in after)


def test_receipt_cache_immutable_and_foreign_board_not_authority(synced_conn):
    conn = synced_conn
    target, _, stop, _, hub = scenario(conn)
    proof = hub.events[stop["event_uid"]]
    replica.remember(conn, proof, board="fixture", task=target, uid=stop["event_uid"])
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        conn.execute("DELETE FROM containment_canonical_receipts")
    altered = copy.deepcopy(proof)
    altered["row"]["origin_gateway"] = "foreign"
    altered["containment_v1"]["gateway"] = "foreign"
    with pytest.raises(ContainmentRefused, match="identity changed"):
        replica.remember(conn, altered, board="fixture", task=target, uid=stop["event_uid"])
    conn.execute("UPDATE kanban_sync_state SET board_id='other'")
    with pytest.raises(ContainmentRefused, match="different board"):
        replica.remember(conn, proof, board="fixture", task=target, uid=stop["event_uid"])


def _released_unheld(conn):
    target, child, stop, release, hub = scenario(conn)
    marker = sync.RemoteChange(1, "tasks", target, "upsert",
                               {"status": "triage", "containment_episode": stop["event_uid"]}, 100, "", "home")
    apply(conn, hub, [marker, event(stop, hub, 2), event(release, hub, 3)])
    assert not connections.held(conn, target)
    return target, child, stop, release, hub


@pytest.mark.parametrize("column,value", [
    ("current_run_id", 1),
    ("human_request", "{}"),
    ("block_kind", "needs_input"),
])
def test_restore_released_does_not_ready_claimed_or_human_held_replica(synced_conn, column, value):
    conn = synced_conn
    target, *_ = _released_unheld(conn)
    conn.execute(f"UPDATE tasks SET status='triage',{column}=? WHERE id=?", (value, target))
    replica._restore_released(conn, target)
    row = conn.execute("SELECT status,current_run_id,human_request,block_kind FROM tasks WHERE id=?",
                       (target,)).fetchone()
    assert row[0] == "triage"
    kept = {"current_run_id": row[1], "human_request": row[2], "block_kind": row[3]}
    assert kept[column] == value


def test_stale_non_upsert_outbox_is_deleted_even_when_nonlifecycle_fields_remain(synced_conn):
    conn = synced_conn
    target, *_ = scenario(conn)
    capture._clear_outbox(conn)
    conn.execute(
        "INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) VALUES (?,?,?,?,?,?,1)",
        ("dead-delete", "tasks", target, "delete", json.dumps({"title": "stale-delete"}), "0"),
    )
    conn.execute(
        "INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) VALUES (?,?,?,?,?,?,1)",
        ("keep-upsert", "tasks", target, "upsert", json.dumps({"title": "keep"}), "0"),
    )
    from hermes_cli.kanban_containment_outbox import strip_lifecycle
    strip_lifecycle(conn, target)
    remaining = {row[0]: row[1] for row in conn.execute(
        "SELECT op_id,op FROM kanban_sync_outbox WHERE row_uid=?", (target,))}
    assert "dead-delete" not in remaining
    assert remaining.get("keep-upsert") == "upsert"


def test_corrupt_containment_delivery_json_holds(synced_conn):
    from hermes_cli import kanban_containment_canonical as canonical
    from hermes_cli import kanban_containment_requests as requests
    conn = synced_conn
    target, *_ = scenario(conn)
    canonical._schema(conn)
    conn.execute(
        "INSERT INTO containment_delivery(request_uid,board,gateway,op_json,confirmed) VALUES (?,?,?,?,0)",
        ("corrupt-req", "fixture", "worker", "{not-json",),
    )
    assert requests.pending(conn, target, set()) is True
    assert connections.held(conn, target)


def test_task_permit_does_not_authorize_a_different_held_uid(synced_conn):
    conn = synced_conn
    target, child, stop, _, hub = scenario(conn)
    marker = sync.RemoteChange(1, "tasks", target, "upsert",
                               {"status": "triage", "containment_episode": stop["event_uid"]}, 100, "", "home")
    apply(conn, hub, [marker])
    assert connections.held(conn, target)
    other = sync.RemoteChange(2, "tasks", child, "upsert", {"status": "todo"}, 101, "", "home")
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        with replica.projection(conn, other):
            with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
                conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (target,))
    assert kb.get_task(conn, target).status == "triage"


def test_replica_restore_keeps_spend_hold_in_triage(synced_conn):
    conn = synced_conn
    target, child, stop, release, hub = scenario(conn)
    marker = sync.RemoteChange(1, "tasks", target, "upsert",
                               {"status": "triage", "containment_episode": stop["event_uid"]}, 100, "", "home")
    apply(conn, hub, [marker, event(stop, hub, 2)])
    with kb.write_txn(conn):
        kb._append_event(conn, target, "spend_budget_triaged", {"fixture": True})
    apply(conn, hub, [event(release, hub, 3)])
    assert not connections.held(conn, target)
    assert kb.get_task(conn, target).status == "triage"
    assert kb.get_task(conn, child).status == "todo"
    assert conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='spend_budget_triaged'",
                        (target,)).fetchone()
