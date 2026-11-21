"""Terminal archive refuses malformed custody and receipt evidence."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_containment_connections as connections
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from tests.hermes_cli import test_kanban_containment_lifecycle as lifecycle
from tests.hermes_cli import test_kanban_containment_terminal_remote as tr

board = lifecycle.board
kanban_home = tr.kanban_home
synced_conn = tr.synced_conn
# ---------------- remote ----------------

def test_r1_rejected_request_is_retired_and_retry_uses_new_identity(synced_conn, monkeypatch):
    hub = tr.setup(synced_conn, monkeypatch)
    real = hub.ops
    state = {"reject": True}

    def ops(board_, o):
        out = real(board_, o)
        if state["reject"]:
            hub.audit = None
            return {"results": [{"client_op_id": o[0]["client_op_id"], "status": "rejected", "reason": "authority_live"}]}
        return out
    hub.ops = ops
    with pytest.raises(ContainmentRefused, match="Board refused"):
        kb.archive_task(synced_conn, hub.target)
    assert synced_conn.execute("SELECT confirmed FROM containment_delivery").fetchall()[0][0] == -1
    assert kb.get_task(synced_conn, hub.target).status == "triage"
    state["reject"] = False
    assert kb.archive_task(synced_conn, hub.target)
    assert hub.posts[0][0]["client_op_id"] != hub.posts[1][0]["client_op_id"]


def test_r2_in_transaction_refused_before_network(synced_conn, monkeypatch):
    hub = tr.setup(synced_conn, monkeypatch)
    with kb.write_txn(synced_conn):
        with pytest.raises(ContainmentRefused, match="writer transaction"):
            kb.archive_task(synced_conn, hub.target)
    assert hub.posts == [] and not hub.closed


def test_r3_ambiguous_custody_refused(synced_conn, monkeypatch):
    from hermes_cli import kanban_containment_canonical as canonical
    hub = tr.setup(synced_conn, monkeypatch)
    canonical._schema(synced_conn)
    for _ in range(2):
        uid = sync.uuid7()
        synced_conn.execute("INSERT INTO containment_delivery(request_uid,board,gateway,op_json,confirmed) VALUES (?,?,?,?,0)",
                            (uid, "fixture", hub.ident.gateway, json.dumps({"op": "archive", "task": hub.target, "client_op_id": uid})))
    with pytest.raises(ContainmentRefused, match="ambiguous"):
        kb.archive_task(synced_conn, hub.target)
    assert hub.posts == []


@pytest.mark.parametrize("field,value", [("containment_episode", None), ("current_run_uid", "run-x"),
                                         ("human_request", '{"action":"x"}'), ("delete_pending", 1)])
def test_r4_task_readback_custody(synced_conn, monkeypatch, field, value):
    hub = tr.setup(synced_conn, monkeypatch)
    hub.task[field] = value
    with pytest.raises(ContainmentRefused) as err:
        kb.archive_task(synced_conn, hub.target)
    assert "readback changed custody" in str(err.value)
    assert kb.get_task(synced_conn, hub.target).status == "triage"
    assert not synced_conn.execute("SELECT 1 FROM containment_delivery WHERE confirmed=1").fetchone()


def test_r6_closure_evidence_not_live(synced_conn, monkeypatch):
    hub = tr.setup(synced_conn, monkeypatch)
    real = hub.row_evidence

    def ev(board_, table, uid):
        out = real(board_, table, uid)
        if uid == hub.close_event["event_uid"]:
            out["state"] = "deleted"
        return out
    hub.row_evidence = ev
    with pytest.raises(ContainmentRefused) as err:
        kb.archive_task(synced_conn, hub.target)
    assert "event evidence is invalid" in str(err.value)
    assert kb.get_task(synced_conn, hub.target).status == "triage"


@pytest.mark.parametrize("via", [None, "another route"])
def test_repeat_archive_is_noop_without_a_second_reason_comment(synced_conn, monkeypatch, via):
    hub = tr.setup(synced_conn, monkeypatch)
    assert kb.archive_task(synced_conn, hub.target, comment=("me", "why"))
    before = synced_conn.total_changes
    assert not kb.archive_task(synced_conn, hub.target, comment=("me", "why"), via=via)
    assert synced_conn.total_changes == before
    assert synced_conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=?", (hub.target,)).fetchone()[0] == 1
    assert synced_conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='archived'", (hub.target,)).fetchone()[0] == 1
    assert len(hub.posts) == 1


def test_r8_creator_scope_target_mismatch(synced_conn, monkeypatch):
    from hermes_cli.kanban_creator_archive import CreatorScope
    hub = tr.setup(synced_conn, monkeypatch)
    creator = kb.create_task(synced_conn, title="creator", assignee="default")
    tr._creator(synced_conn, hub, creator)
    with pytest.raises(ContainmentRefused, match="creator_archive requires"):
        kb.archive_task(synced_conn, hub.target, precondition=CreatorScope(hub.child, creator))
    assert hub.posts == []


def test_r9_unknown_verdict(synced_conn, monkeypatch):
    from hermes_cli.kanban_containment_canonical import DeliveryUnknown
    hub = tr.setup(synced_conn, monkeypatch)
    real = hub.ops
    hub.ops = lambda b, o: {"results": [{**real(b, o)["results"][0], "status": "queued"}]}
    with pytest.raises(DeliveryUnknown):
        kb.archive_task(synced_conn, hub.target)
    assert [tuple(r) for r in synced_conn.execute("SELECT confirmed FROM containment_delivery")] == [(0,)]


@pytest.mark.parametrize("shape", ["dup", "notlist", "empty-uid"])
def test_r10_context_shape(synced_conn, monkeypatch, shape):
    hub = tr.setup(synced_conn, monkeypatch)
    orig = hub.containment_context

    def ctx(*a, **k):
        out = orig(*a, **k)
        ev = out["containment_events"]
        out["containment_events"] = {"dup": ev + ev[:1], "notlist": tuple(ev), "empty-uid": ev + [""]}[shape]
        return out
    hub.containment_context = ctx
    with pytest.raises(ContainmentRefused, match="context is incomplete"):
        kb.archive_task(synced_conn, hub.target)
    assert kb.get_task(synced_conn, hub.target).status == "triage"


# ---------------- local ----------------

def _held(board):
    relay, consumer, authority, target, spec = lifecycle.setup(board)
    episode = lifecycle.contain(board, relay, target, spec)["episode"]
    assert kb.archive_task(board, authority)
    return relay, consumer, authority, target, spec, episode


@pytest.mark.parametrize("col,value", [("human_request", '{"action":"x"}'), ("claim_lock", "someone"),
                                       ("claim_expires", 99), ("delete_pending", 1)])
def test_l1_local_custody_columns(board, col, value):
    relay, *_ = _held(board)
    if col not in {r[1] for r in board.execute("PRAGMA table_info(tasks)")}:
        board.execute(f"ALTER TABLE tasks ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
    board.execute(f"UPDATE tasks SET {col}=? WHERE id=?", (value, relay))
    board.commit()
    with pytest.raises(ContainmentRefused, match="not an unclaimed triage task"):
        kb.archive_task(board, relay)
    assert kb.get_task(board, relay).status == "triage"


def test_l2_null_marker(board):
    from hermes_cli import kanban_containment_guard as guard
    relay, *_ = _held(board)
    with kb.write_txn(board), guard.authorized_write(board, relay):
        board.execute("UPDATE tasks SET containment_episode=NULL WHERE id=?", (relay,))
    assert connections.held(board, relay)
    with pytest.raises(ContainmentRefused):
        kb.archive_task(board, relay)


@pytest.mark.parametrize("sql", [
    "UPDATE tasks SET status='archived', current_run_id=7 WHERE id=?",
    "UPDATE tasks SET status='archived', completed_at=7 WHERE id=?",
    "UPDATE tasks SET status='archived', containment_episode=NULL WHERE id=?",
    "UPDATE tasks SET status='archived', containment_episode='other' WHERE id=?",
    "UPDATE tasks SET status='done' WHERE id=?",
])
def test_l4_terminal_writer_scope(board, sql):
    from hermes_cli.kanban_containment_terminal import writer
    relay, *_ = _held(board)
    with kb.write_txn(board), writer(board, relay):
        with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
            board.execute(sql, (relay,))


def test_l5_local_stop_forgery(board):
    relay, *_ = _held(board)
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        board.execute("INSERT INTO containment_local_stops VALUES (?,?,?)", (relay, sync.uuid7(), "x"))


@pytest.mark.parametrize("fault", ["version_str", "version_bool", "version_2", "cross_task", "done_with_producer", "extra_key"])
def test_l6_closure_payload(board, fault):
    from hermes_cli.kanban_containment_terminal import closure, terminal_closed
    relay, consumer, _authority, target, _spec, _episode = _held(board)
    assert kb.archive_task(board, relay)
    events = [dict(r) for r in board.execute("SELECT * FROM task_events WHERE task_id=?", (relay,))]
    stop = next(e for e in events if e["kind"] == "contained")
    close = next(e for e in events if e["kind"] == "containment_closed")
    assert closure(close, stop)
    data = json.loads(close["payload"])
    stop2 = dict(stop)
    versions = {"version_str": "1", "version_bool": True, "version_2": 2}
    if fault in versions:
        data["version"] = versions[fault]
    elif fault == "cross_task":
        close["task_id"] = consumer
    elif fault == "extra_key":
        data["more"] = 1
    else:
        frozen = json.loads(stop["payload"])
        frozen["policy"]["producer"] = {**target, "task_id": consumer}
        stop2["payload"] = json.dumps(frozen)
        from hermes_cli.kanban_containment_state import _stop, fingerprint
        data["containment_sha256"] = fingerprint(_stop(stop2))
        data["authority_status"], data["reason"] = "done", "authority_done_without_producer"
    close["payload"] = json.dumps(data)
    assert not closure(close, stop2)
    evs = [stop2 if e["kind"] == "contained" else close if e["kind"] == "containment_closed" else e for e in events]
    assert not terminal_closed(evs, verify=lambda *_: True)


def test_projection_must_land_before_delivery_is_confirmed(synced_conn, monkeypatch):
    hub = tr.setup(synced_conn, monkeypatch)
    # Exercise a dropped canonical projection, not malformed Board evidence.
    real_apply = sync.apply_remote_changes
    def audit_only(conn, changes, **kwargs):
        return real_apply(conn, [c for c in changes if c.table == "task_events" and c.fields.get("kind") == "archived"], **kwargs)
    monkeypatch.setattr(sync, "apply_remote_changes", audit_only)
    with pytest.raises(ContainmentRefused, match="was not projected locally"):
        kb.archive_task(synced_conn, hub.target, comment=("owner", "why"))
    assert kb.get_task(synced_conn, hub.target).status == "triage"
    assert not synced_conn.execute("SELECT 1 FROM containment_delivery WHERE confirmed=1").fetchone()
    assert not synced_conn.execute("SELECT 1 FROM task_comments WHERE task_id=?", (hub.target,)).fetchone()


def test_l7_closed_with_unknown_observation(board):
    from hermes_cli.kanban_containment_terminal import closed
    relay, *_ = _held(board)
    assert kb.archive_task(board, relay)
    assert closed(board, relay)
    board.execute("INSERT INTO containment_observations(task_id,episode) VALUES (?,?)", (relay, sync.uuid7()))
    assert not closed(board, relay)
