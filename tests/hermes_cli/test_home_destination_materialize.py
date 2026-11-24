"""Destination ACK means rows actually committed, not a manifest echo."""
from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from tests.hermes_cli import test_home_transfer_capture as support

board = support.board


def manifest(task="t_destination00001") -> dict:
    def entry(table, uid, **fields):
        return dict(table=table, uid=uid, fields=dict(uid=uid, seq=4, version=1,
                    updated_at=1000, hlc="1000-0-server", origin_gateway="worker-a3", deleted_at=None, **fields))
    record = dict(task=task, source="worker-a3", destination="hub", nonce="destination-000001",
                  expected_version=7, phase="committed")
    row = entry("tasks", task, id=task, title="portable", body="retained", status="scheduled", created_at=1,
                created_on="hub", runs_on='["pool:worker"]', home_generation=record["nonce"])
    row["fields"]["version"] = 8
    rows = [row,
        entry("task_comments", task+"-comment", task_id=task, author="actor", body="history", created_at=1),
        entry("task_events", task+"-event", task_id=task, ord=1, kind="promoted_manual", payload='{"keep":"inert"}', created_at=1),
        entry("task_runs", task+"-run", task_id=task, profile="default", status="done", started_at=1, ended_at=2),
        entry("task_links", "incoming>"+task, parent_id="incoming", child_id=task),
        entry("task_links", task+">outgoing", parent_id=task, child_id="outgoing"),
        entry("task_attachments", task+"-deleted", task_id=task, filename="deleted.txt", size=0, created_at=1)]
    rows[-1]["fields"]["deleted_at"] = 1000
    return dict(record, rows=rows)


def prepare_destination(conn):
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET gateway_id='hub'")
        conn.execute("DELETE FROM kanban_sync_outbox")


def materialize(conn, record):
    from hermes_cli.kanban_home_destination import materialize as apply
    with kbc.write_txn(conn):
        return apply(conn, record)


def test_full_manifest_materializes_durably_without_replaying_status_or_cursor(board):
    prepare_destination(board)
    before = board.execute("SELECT cursor FROM kanban_sync_state").fetchone()[0]
    record = manifest()
    assert materialize(board, record) == record["rows"]
    task = kb.get_task(board, record["task"])
    assert task is not None and task.status == "scheduled"
    assert board.execute("SELECT created_on FROM tasks WHERE id=?", (task.id,)).fetchone()[0] == "hub"
    assert board.execute("SELECT count(*) FROM task_events WHERE task_id=?", (task.id,)).fetchone()[0] == 1
    assert board.execute("SELECT count(*) FROM task_links WHERE parent_id=? OR child_id=?", (task.id,task.id)).fetchone()[0] == 2
    assert board.execute("SELECT 1 FROM kanban_sync_tombstones WHERE row_uid=?", (task.id+"-deleted",)).fetchone()
    assert board.execute("SELECT count(*) FROM kanban_sync_outbox").fetchone()[0] == 0
    assert board.execute("SELECT cursor FROM kanban_sync_state").fetchone()[0] == before
    path = board.execute("PRAGMA database_list").fetchone()[2]
    with sqlite3.connect(path) as peer:
        receipt = peer.execute("SELECT manifest FROM kanban_home_materialized1 WHERE task_id=?", (task.id,)).fetchone()
        assert json.loads(receipt[0]) == record
        assert peer.execute("SELECT nonce FROM kanban_home_generations1 WHERE task_id=?", (task.id,)).fetchone()[0] == record["nonce"]


def test_manifest_import_resolves_runs_before_events_and_orders_events_causally(board):
    prepare_destination(board)
    record = manifest()
    event = record["rows"][2]
    run = record["rows"][3]
    event["uid"] = event["fields"]["uid"] = "a-later-event"
    event["fields"].update(ord=2, run_uid=run["uid"])
    earlier = copy.deepcopy(event)
    earlier["uid"] = earlier["fields"]["uid"] = "z-earlier-event"
    earlier["fields"].update(ord=1)
    record["rows"].insert(3, earlier)  # actual Board snapshots sort event UIDs
    materialize(board, record)
    events = board.execute("SELECT uid,run_id FROM task_events WHERE task_id=? ORDER BY id", (record["task"],)).fetchall()
    run_id = board.execute("SELECT id FROM task_runs WHERE uid=?", (run["uid"],)).fetchone()[0]
    assert [tuple(row) for row in events] == [("z-earlier-event",run_id),("a-later-event",run_id)]


def test_existing_event_ids_with_conflicting_order_refuse_ack_without_rewriting(board):
    prepare_destination(board)
    record = manifest()
    later = record["rows"][2]
    later["fields"]["ord"] = 2
    earlier = copy.deepcopy(later)
    earlier["uid"] = earlier["fields"]["uid"] = "earlier-event"
    earlier["fields"]["ord"] = 1
    record["rows"].insert(3, earlier)
    # An existing feed replica can already have allocated local IDs in UID order.
    changes = [sync.RemoteChange(seq=i+1, table=r["table"], uid=r["uid"],
        op="delete" if r["fields"].get("deleted_at") is not None else "upsert", fields=r["fields"],
        version=r["fields"]["version"], hlc=r["fields"]["hlc"], origin_gateway=r["fields"]["origin_gateway"])
        for i,r in enumerate(record["rows"])]
    sync.apply_remote_changes(board, changes, new_cursor=len(changes), observers=False)
    before = [tuple(row) for row in board.execute("SELECT id,uid FROM task_events ORDER BY id")]
    with pytest.raises(ValueError, match="event chronology"):
        materialize(board, record)
    assert [tuple(row) for row in board.execute("SELECT id,uid FROM task_events ORDER BY id")] == before
    assert board.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_home_materialized1'").fetchone() is None


def test_import_failure_rolls_back_task_epoch_children_and_ack_even_when_caught(board):
    prepare_destination(board)
    with kbc.write_txn(board):
        board.execute("CREATE TRIGGER fail_destination BEFORE INSERT ON task_comments BEGIN SELECT RAISE(ABORT,'injected import failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="injected import failure"):
            from hermes_cli.kanban_home_destination import materialize as apply
            apply(board, manifest())
    assert kb.get_task(board,"t_destination00001") is None
    assert board.execute("SELECT count(*) FROM kanban_home_generations1").fetchone()[0] == 0
    assert board.execute("SELECT applying FROM kanban_sync_state").fetchone()[0] == 0
    assert board.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_home_materialized1'").fetchone() is None


def test_retry_rechecks_real_rows_not_just_saved_ack_and_preserves_later_work(board):
    prepare_destination(board)
    record = manifest()
    materialize(board, record)
    assert materialize(board, record) == record["rows"]
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET title='corrupt local projection' WHERE id=?", (record["task"],))
        board.execute("DELETE FROM kanban_sync_outbox")
    with pytest.raises(ValueError, match="materialization"):
        materialize(board, record)
    assert kb.get_task(board, record["task"]).title == "corrupt local projection"


@pytest.mark.parametrize("mutation", ["wrong-gateway", "uncommitted", "bad-version", "bad-generation", "unrelated", "duplicate"])
def test_invalid_manifest_or_wrong_actor_leaves_no_ack(board, mutation):
    prepare_destination(board)
    record = manifest()
    if mutation == "wrong-gateway":
        with kbc.write_txn(board):
            board.execute("UPDATE kanban_sync_state SET gateway_id='worker-a2'")
    edits = {
        "uncommitted": (record, "phase", "prepared"),
        "bad-version": (record["rows"][0]["fields"], "version", 9),
        "bad-generation": (record["rows"][0]["fields"], "home_generation", "another-generation"),
        "unrelated": (record["rows"][1]["fields"], "task_id", "unrelated"),
    }
    if mutation in edits:
        target, key, value = edits[mutation]
        target[key] = value
    if mutation == "duplicate":
        record["rows"].append(copy.deepcopy(record["rows"][1]))
    with pytest.raises(ValueError):
        materialize(board, record)
    assert kb.get_task(board, record["task"]) is None


def test_pending_destination_work_is_not_overwritten_or_silently_acked(board):
    prepare_destination(board)
    task = kb.create_task(board, title="pending local intent")
    record = manifest(task)
    with pytest.raises(ValueError, match="pending"):
        materialize(board, record)
    assert kb.get_task(board, task).title == "pending local intent"


@pytest.mark.parametrize("historical", [True, False])
def test_primary_destination_import_does_not_create_new_source_or_restore_intents(board, historical):
    prepare_destination(board)
    with kbc.write_txn(board):
        board.execute("UPDATE kanban_sync_state SET mode='primary'")
    record = manifest()
    event = record["rows"][2]["fields"]
    # Even an invalid historical intent must remain historical; normal feed
    # insertion would manufacture a new local invalid/pending source intent.
    event["payload"] = json.dumps({"actionability_intent": {"version": 1}})
    record["rows"].append(dict(table="task_chat_links", uid="original-shepherd-link", fields=dict(
        uid="original-shepherd-link", version=1, hlc="1000-0-server", origin_gateway="worker-a3",
        deleted_at=None, task_id=record["task"], chat_gateway="macbook", chat_profile="default",
        chat_session="20261123_230155_1b3bb9", kind="shepherd", released=0, created_at=1)))
    if historical:
        materialize(board, record)
    else:
        # The ordinary feed retains its real restore/capture semantics. Only
        # the explicit handoff import is inert; applying=1 alone is not enough.
        changes = [sync.RemoteChange(seq=i + 1, table=r["table"], uid=r["uid"],
            op="delete" if r["fields"].get("deleted_at") is not None else "upsert", fields=r["fields"],
            version=r["fields"]["version"], hlc=r["fields"]["hlc"], origin_gateway=r["fields"]["origin_gateway"])
            for i, r in enumerate(record["rows"])]
        sync.apply_remote_changes(board, changes, new_cursor=len(changes), observers=False)
    for table in ("kanban_restored_cards", "kanban_notify_subs", "kanban_actionability_intents"):
        exists = board.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        count = board.execute(f"SELECT count(*) FROM {table}").fetchone()[0] if exists else 0
        assert count == (0 if historical else 1), table
    assert kb.get_task(board, record["task"]).status == "scheduled"


@pytest.mark.parametrize("certified", [False, True])
def test_contained_history_requires_ordinary_canonical_evidence_and_keeps_hold(board, certified):
    from hermes_cli import kanban_containment_replica as replica
    from hermes_cli import kanban_containment_connections as connections
    from tests.hermes_cli import test_kanban_containment_canonical as wire

    prepare_destination(board)
    sync.set_identity(board, "fixture", "hub")
    op, stop, episode = wire.operation()
    record = manifest(wire.TASK)
    record["rows"][0]["fields"].update(status="triage", containment_episode=episode)
    hub = wire.Hub(op, stop, episode)
    # The transport double supplies canonical envelope bytes, not a production
    # attestation. Matching receipt verification remains ordinary admission.
    fields = dict(hub.events[episode]["row"], version=1, hlc="1000-0-server", deleted_at=None, created_at=1, ord=2)
    hub.events[episode]["row"] = copy.deepcopy(fields)
    record["rows"].append(dict(table="task_events", uid=episode, fields=fields))
    change = sync.RemoteChange(seq=1, table="task_events", uid=episode, op="upsert", fields=fields,
        version=1, hlc=fields["hlc"], origin_gateway=fields["origin_gateway"])
    if not certified:
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            materialize(board, record)
        assert kb.get_task(board, wire.TASK) is None
        assert board.execute("SELECT count(*) FROM kanban_home_generations1").fetchone()[0] == 0
        return
    replica.certify_page(board, hub, "fixture", [change])
    materialize(board, record)
    assert connections.held(board, wire.TASK)
    assert kb.get_task(board, wire.TASK).status == "triage"
    assert kb.get_task(board, wire.TASK).containment_episode == episode
    assert board.execute("SELECT evidence FROM containment_canonical_receipts WHERE uid=?", (episode,)).fetchone()
    assert board.execute("SELECT count(*) FROM task_links WHERE parent_id=? OR child_id=?", (wire.TASK,wire.TASK)).fetchone()[0] == 2
