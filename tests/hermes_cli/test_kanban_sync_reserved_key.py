"""Explicit reserved-idempotency-key correction of a never-created local card (kanban_sync_reserved_key)."""
import json
from argparse import Namespace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_creation as creation
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_sync_reserved_key as rk
from hermes_cli.kanban_pickup_alarm_identity import DETECTOR, reserved
from hermes_cli.kanban_sync_rejections import resolve_rejections
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

BOARD, ME, OTHER = "default", "macbook", "mini"
BAD, GOOD = "pickup-stall:mini:disk:1", "ordinary-key-1"
REASON = "operator fixes never-created card"


class StrictService(FakeService):
    """The Board's fence: a reserved key from a non-detector refuses the card; its children have no card."""

    def row_op(self, b, op, gw):
        table, f = op["table"], op.get("fields") or {}
        if op["op"] != "delete":
            if table == "tasks" and reserved(f.get("idempotency_key")) and f.get("created_by") != DETECTOR:
                return {"status": "rejected", "reason": "invalid", "detail": "pickup_stall_provenance"}
            ends = [f.get("parent_id"), f.get("child_id")] if table == "task_links" else [f.get("task_id")]
            if table != "tasks" and any(e and e not in b["rows"].get("tasks", {}) for e in ends):
                return {"status": "rejected", "reason": "not_found", "detail": "parent card is missing"}
        return super().row_op(b, op, gw)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return StrictService()


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    client.ensure_identity(conn, BOARD, ME)
    try:
        yield conn
    finally:
        conn.close()


def quiet(conn, sql, args=()):
    """A local write the capture triggers do not see (fixture state, not an ordinary edit)."""
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET applying=1")
        conn.execute(sql, args)
        conn.execute("UPDATE kanban_sync_state SET applying=0")


def bad_card(live, svc, monkeypatch, *, key=BAD, created_by="user", title="ordinary work", push=True):
    """A card created with a reserved key before the create-time refusal existed, whose ops the Board refused."""
    with monkeypatch.context() as m:
        m.setattr(creation, "reject_reserved_key", lambda *a, **k: None)
        other = kb.create_task(live, title="unrelated", assignee="worker")
        tid = kb.create_task(live, title=title, assignee="worker", created_by=created_by, idempotency_key=key)
    kb.add_comment(live, tid, "user", "a comment that must survive")
    with kbc.write_txn(live):
        live.execute("INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
                     "VALUES (?, 'worker', 'done', 1, 2, 'completed')", (tid,))
        live.execute("INSERT INTO task_chat_links (task_id, chat_gateway, chat_session, kind, created_at) "
                     "VALUES (?, ?, 's1', 'created', 1)", (tid, ME))
        live.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (other, tid))
    if push:
        client.push(live, svc.hub(ME), BOARD, ME, primary=False)
        assert svc.board(BOARD)["rows"].get("tasks", {}).get(tid) is None
    else:  # nothing was sent: no op is rejected yet, so mark them as a refused push would
        quiet(live, "UPDATE kanban_sync_outbox SET state='rejected', last_error='invalid: pickup_stall_provenance' "
                    "WHERE state='pending'")
    return tid, other


def rejected_ops(conn, tid=None):
    return [dict(r) for r in conn.execute("SELECT * FROM kanban_sync_outbox WHERE state='rejected' ORDER BY op_id")]


def key_op(conn, key=BAD):
    return next(o["op_id"] for o in rejected_ops(conn) if o["table_name"] == "tasks"
                and json.loads(o["fields"]).get("idempotency_key") == key)


def dump(conn):
    return list(conn.iterdump())


def preview(live, svc, tid, key=GOOD):
    return rk.plan(live, svc.hub(ME), BOARD, tid, key, REASON)


def apply(live, svc, tid, token, key=GOOD):
    return rk.apply(live, svc.hub(ME), BOARD, tid, key, token, REASON)


def rounds(live, svc, kanban_home, n=5):
    for _ in range(n):
        client.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)


# --- creation refusal ---------------------------------------------------------------------------------

def test_ordinary_create_refuses_reserved_key_before_any_write_but_detector_works(live):
    before = dump(live)
    for key in ("pickup-stall:mini:x:1", "worker-resources-unknown:mini:1", "pickup-stall:"):
        with pytest.raises(ValueError, match="reserved"):
            kb.create_task(live, title="t", assignee="worker", created_by="user", idempotency_key=key)
    assert dump(live) == before
    d1 = kb.create_task(live, title="d1", assignee="worker", created_by=DETECTOR, idempotency_key="pickup-stall:mini:disk:7")
    d2 = kb.create_task(live, title="d2", assignee="worker", created_by=DETECTOR,
                        idempotency_key="worker-resources-unknown:mini:9")
    assert d1 != d2
    assert kb.create_task(live, title="ok", assignee="worker", idempotency_key="my-key")


# --- preview ----------------------------------------------------------------------------------------

def test_preview_is_read_only_and_names_exact_old_ops(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    old = rejected_ops(live)
    assert old
    before = dump(live)
    report = preview(live, svc, tid)
    assert report["eligible"] and report["action"] == "preview" and report["token"]
    assert sorted(report["superseded_op_ids"]) == sorted(o["op_id"] for o in old)
    kinds = {t for t, _u in report["children"]}
    assert {"task_comments", "task_events", "task_runs", "task_chat_links", "task_links"} <= kinds
    assert dump(live) == before  # nothing written, not even the receipt tables


# --- apply -------------------------------------------------------------------------------------------

def test_apply_corrects_key_keeps_rejections_and_history_then_children_follow_parent(live, svc, monkeypatch, kanban_home):
    tid, other = bad_card(live, svc, monkeypatch)
    old = {o["op_id"]: o for o in rejected_ops(live)}
    events_before = [tuple(r) for r in live.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (tid,))]
    other_before = [tuple(r) for r in live.execute("SELECT * FROM tasks WHERE id=?", (other,))]
    token = preview(live, svc, tid)["token"]
    result = apply(live, svc, tid, token)
    assert result["action"] == "applied"
    assert live.execute("SELECT idempotency_key FROM tasks WHERE id=?", (tid,)).fetchone()[0] == GOOD
    # Immutable: every rejected op byte-identical, same ids, nothing deleted or reset.
    assert {o["op_id"]: o for o in rejected_ops(live)} == old
    assert [tuple(r) for r in live.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (tid,))] == events_before
    # Exactly one new pending op: the corrected parent; children wait for its acceptance.
    pending = [dict(r) for r in live.execute("SELECT * FROM kanban_sync_outbox WHERE state='pending'")]
    assert [(p["table_name"], p["row_uid"], p["op"]) for p in pending] == [("tasks", tid, "import")]
    assert json.loads(pending[0]["fields"])["idempotency_key"] == GOOD
    receipt = rk.receipt_for_task(live, tid)
    assert receipt["state"] == "parent_queued" and receipt["old_key"] == BAD and receipt["new_key"] == GOOD
    mapped = {r[0] for r in live.execute("SELECT old_op_id FROM kanban_sync_key_supersession_ops")}
    assert mapped == set(old)
    assert [tuple(r) for r in live.execute("SELECT * FROM tasks WHERE id=?", (other,))] == other_before

    calls_before = len(svc.calls)
    rounds(live, svc, kanban_home)
    ops_sent = [(i, o) for i, (kind, _b, body) in enumerate(svc.calls[calls_before:]) if kind == "ops" for o in body]
    parent_at = next(i for i, o in ops_sent if o["table"] == "tasks" and o["uid"] == tid
                     and o["fields"].get("idempotency_key") == GOOD)
    child_at = [i for i, o in ops_sent if o["table"] != "tasks" and o["uid"] not in old]
    assert child_at and all(i > parent_at for i in child_at)  # children only after the accepted parent round
    board = svc.board(BOARD)["rows"]
    assert board["tasks"][tid]["idempotency_key"] == GOOD and board["tasks"][tid]["created_on"] == ME
    assert board["task_comments"] and board["task_chat_links"] and board["task_runs"]
    assert rk.receipt_for_task(live, tid)["state"] == "complete"
    assert not [r for r in live.execute("SELECT 1 FROM kanban_sync_outbox WHERE state='pending'")]
    # The old rejections retire only through the receipt, which keeps their exact bytes.
    assert client.status(live)["rejected"] == 0
    kept = {r[0]: json.loads(r[1]) for r in live.execute("SELECT old_op_id, op_json FROM kanban_sync_key_supersession_ops")}
    assert kept == old
    assert [tuple(r) for r in live.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (tid,))][:len(events_before)] \
        == events_before


def test_repeated_apply_is_idempotent_and_continues_receipt(live, svc, monkeypatch, kanban_home):
    tid, _ = bad_card(live, svc, monkeypatch)
    token = preview(live, svc, tid)["token"]
    assert apply(live, svc, tid, token)["action"] == "applied"
    outbox = dump(live)
    again = apply(live, svc, tid, token)
    assert again["action"] == "already_applied" and again["advance"][0]["advanced"] is False
    assert dump(live) == outbox
    assert apply(live, svc, tid, token, key="another-key")["refusal"] == "receipt_exists_other_key"
    client.push(live, svc.hub(ME), BOARD, ME, primary=False)
    client._pull_replica(BOARD, svc.hub(ME), ME)
    third = apply(live, svc, tid, token)  # a repeat after acceptance performs the child step
    assert third["action"] == "already_applied" and third["advance"][0]["advanced"]
    count = live.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE state='pending'").fetchone()[0]
    assert apply(live, svc, tid, token)["advance"][0]["advanced"] is False  # children queued, not yet delivered
    assert live.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE state='pending'").fetchone()[0] == count


def test_crash_inside_apply_leaves_no_partial_state(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    token = preview(live, svc, tid)["token"]
    before = dump(live)

    def boom(*a, **k):
        raise RuntimeError("crash after the key rewrite")

    monkeypatch.setattr(client, "_row_fields", boom)
    with pytest.raises(RuntimeError):
        apply(live, svc, tid, token)
    monkeypatch.undo()
    assert dump(live) == before
    assert apply(live, svc, tid, token)["action"] == "applied"  # the same token still applies after the crash


def test_crash_between_accepted_parent_and_children_resumes(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    apply(live, svc, tid, preview(live, svc, tid)["token"])
    client.push(live, svc.hub(ME), BOARD, ME, primary=False)
    real = sync.uuid7
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("crash while queueing children")
        return real()

    monkeypatch.setattr(sync, "uuid7", flaky)
    with pytest.raises(RuntimeError):
        rk.advance(live, svc.hub(ME), BOARD)
    monkeypatch.setattr(sync, "uuid7", real)
    assert rk.receipt_for_task(live, tid)["state"] == "parent_queued"
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE state='pending'").fetchone()
    assert rk.advance(live, svc.hub(ME), BOARD)[0]["advanced"]
    assert rk.receipt_for_task(live, tid)["state"] == "children_queued"
    pending = [tuple(r) for r in live.execute("SELECT table_name, row_uid FROM kanban_sync_outbox WHERE state='pending'")]
    assert len(pending) == len(set(pending)) > 0


def test_children_wait_while_board_has_not_accepted_parent(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    apply(live, svc, tid, preview(live, svc, tid)["token"])
    assert "parent op" in rk.advance(live, svc.hub(ME), BOARD)[0]["waiting"]  # still queued
    quiet(live, "DELETE FROM kanban_sync_outbox WHERE state='pending'")  # op gone but nothing acknowledged it
    assert "not acknowledged" in rk.advance(live, svc.hub(ME), BOARD)[0]["waiting"]
    quiet(live, "UPDATE tasks SET sync_version=5 WHERE id=?", (tid,))
    assert "has not accepted" in rk.advance(live, svc.hub(ME), BOARD)[0]["waiting"]  # Board never saw the card
    assert rk.receipt_for_task(live, tid)["state"] == "parent_queued"


# --- refusals ---------------------------------------------------------------------------------------

def assert_refused(live, svc, tid, code, key=GOOD):
    before = dump(live)
    report = preview(live, svc, tid, key)
    assert not report["eligible"] and report["refusal"].startswith(code), report
    result = apply(live, svc, tid, "anything", key)
    assert result["action"] == "refused" and dump(live) == before


def test_detector_card_is_never_corrected(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch, key="pickup-stall:macbook:disk:3", created_by=DETECTOR, push=False)
    assert_refused(live, svc, tid, "detector_card")


def test_ordinary_key_card_refused(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch, key="plain", push=False)
    assert_refused(live, svc, tid, "key_not_reserved")


def test_foreign_or_acknowledged_card_refused(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    quiet(live, "UPDATE tasks SET created_on=? WHERE id=?", (OTHER, tid))
    assert_refused(live, svc, tid, "not_locally_owned")
    quiet(live, "UPDATE tasks SET created_on=NULL, sync_version=3 WHERE id=?", (tid,))
    assert_refused(live, svc, tid, "not_locally_owned")


def test_foreign_gateway_identity_refused(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    report = rk.plan(live, svc.hub(OTHER), BOARD, tid, GOOD, REASON)
    assert report["refusal"] == "foreign_gateway_identity"


@pytest.mark.parametrize("fixture", ["running", "claim", "lease", "open_run", "current_run", "worker_pid"])
def test_running_or_leased_card_refused(live, svc, monkeypatch, fixture):
    tid, _ = bad_card(live, svc, monkeypatch)
    sql = {"running": "UPDATE tasks SET status='running' WHERE id=?",
           "claim": "UPDATE tasks SET claim_lock='h:1' WHERE id=?",
           "lease": "UPDATE tasks SET lease_holder='mini', lease_expires=9999999999999 WHERE id=?",
           "worker_pid": "UPDATE tasks SET worker_pid=77 WHERE id=?",
           "current_run": "UPDATE tasks SET current_run_id=(SELECT id FROM task_runs WHERE task_id=tasks.id) WHERE id=?",
           "open_run": "UPDATE task_runs SET ended_at=NULL WHERE task_id=?"}[fixture]
    quiet(live, sql, (tid,))
    assert_refused(live, svc, tid, "active_run_or_lease")


def test_claim_racing_the_evidence_read_is_refused_by_transactional_revalidation(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    token = preview(live, svc, tid)["token"]
    hub = svc.hub(ME)
    original = hub.row_evidence
    calls = []

    def read(*a):
        out = original(*a)
        if not calls:  # a worker claims the card after the first evidence read, before the write
            calls.append(1)
            quiet(live, "UPDATE tasks SET status='running', claim_lock='w:1' WHERE id=?", (tid,))
        return out

    monkeypatch.setattr(hub, "row_evidence", read)
    result = rk.apply(live, hub, BOARD, tid, GOOD, token, REASON)
    assert result["action"] == "refused" and "local_state_changed" in result["refusal"]
    assert live.execute("SELECT idempotency_key FROM tasks WHERE id=?", (tid,)).fetchone()[0] == BAD
    assert rk.receipt_for_task(live, tid) is None


def test_write_window_race_vetoed_inside_transaction(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    token = preview(live, svc, tid)["token"]
    # Another process edits the card after plan finished and before the write transaction opens.
    other = kbc.connect(Path(live.execute("PRAGMA database_list").fetchone()[2]))
    real_plan = rk.plan

    def plan_then_edit(*a, **k):
        out = real_plan(*a, **k)
        with kbc.write_txn(other):  # untracked edit: only the token can notice it (no outbox op appears)
            other.execute("UPDATE kanban_sync_state SET applying=1")
            other.execute("UPDATE tasks SET title='edited after plan' WHERE id=?", (tid,))
            other.execute("UPDATE kanban_sync_state SET applying=0")
        return out

    monkeypatch.setattr(rk, "plan", plan_then_edit)
    try:
        result = rk.apply(live, svc.hub(ME), BOARD, tid, GOOD, token, REASON)
    finally:
        other.close()
    assert result["action"] == "refused" and result["refusal"] == "local_state_changed_before_write"
    assert live.execute("SELECT idempotency_key FROM tasks WHERE id=?", (tid,)).fetchone()[0] == BAD
    assert rk.receipt_for_task(live, tid) is None


@pytest.mark.parametrize("mutate", ["title", "comment", "outbox_state"])
def test_stale_cas_refused_without_writing(live, svc, monkeypatch, mutate):
    tid, _ = bad_card(live, svc, monkeypatch)
    token = preview(live, svc, tid)["token"]
    if mutate == "title":
        quiet(live, "UPDATE tasks SET title='moved on' WHERE id=?", (tid,))
    elif mutate == "comment":
        quiet(live, "INSERT INTO task_comments (task_id, author, body, created_at, uid) VALUES (?, 'u', 'new', 5, 'c-new')",
              (tid,))
    else:
        quiet(live, "UPDATE kanban_sync_outbox SET last_error='changed' WHERE state='rejected'")
    before = dump(live)
    result = apply(live, svc, tid, token)
    assert result["action"] == "refused" and result["refusal"] == "stale_or_missing_expected_token"
    assert dump(live) == before
    assert apply(live, svc, tid, None)["refusal"] == "stale_or_missing_expected_token"


@pytest.mark.parametrize("fixture", ["pending", "tombstone", "child_pending"])
def test_pending_outbox_or_tombstone_refused(live, svc, monkeypatch, fixture):
    tid, _ = bad_card(live, svc, monkeypatch)
    with kbc.write_txn(live):
        if fixture == "pending":
            live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) "
                         "VALUES ('p1','tasks',?,'upsert','{}','',2)", (tid,))
        elif fixture == "child_pending":
            live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) "
                         "VALUES ('p1','task_comments','c-x','upsert',?,'',2)", (json.dumps({"task_id": tid}),))
        else:
            comment = live.execute("SELECT uid FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0]
            live.execute("INSERT INTO kanban_sync_tombstones VALUES ('task_comments',?,1)", (comment,))
            live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at,state) "
                         "VALUES ('d1','task_comments',?,'delete',?,'',2,'rejected')", (comment, json.dumps({})))
    assert_refused(live, svc, tid, "pending_outbox" if fixture != "tombstone" else "local_tombstone")


@pytest.mark.parametrize("veto", ["pruned", "live_task", "live_child", "deleted_child", "offline", "identity", "tombstone"])
def test_board_evidence_must_prove_never_existed(live, svc, monkeypatch, veto):
    tid, _ = bad_card(live, svc, monkeypatch)
    rows = svc.board(BOARD)["rows"]
    comment = live.execute("SELECT uid FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0]
    hub = svc.hub(ME)
    original = hub.row_evidence
    veto_table = {
        "pruned": lambda: svc.board(BOARD).__setitem__("floor", 1),
        "live_task": lambda: rows.setdefault("tasks", {}).__setitem__(tid, {"uid": tid, "version": 1}),
        "live_child": lambda: rows.setdefault("task_comments", {}).__setitem__(comment, {"uid": comment, "version": 1}),
        "deleted_child": lambda: rows.setdefault("task_comments", {}).__setitem__(
            comment, {"uid": comment, "deleted_at": 4}),
        "offline": lambda: setattr(svc, "offline", True),
        "identity": lambda: monkeypatch.setattr(hub, "row_evidence", lambda *a: {**original(*a), "uid": "another"}),
        "tombstone": lambda: monkeypatch.setattr(hub, "row_evidence",
                                                 lambda *a: {**original(*a), "tombstone": {"deleted_at": 1}}),
    }
    veto_table[veto]()
    before = dump(live)
    report = rk.plan(live, hub, BOARD, tid, GOOD, REASON)
    assert not report["eligible"] and report["refusal"].split(":")[0] in (
        "board_history_not_proven_never_existed", "authoritative_evidence_unavailable")
    assert rk.apply(live, hub, BOARD, tid, GOOD, "x", REASON)["action"] == "refused"
    assert dump(live) == before


@pytest.mark.parametrize("key", ["", " padded ", "pickup-stall:mini:x:2", "worker-resources-unknown:mini:2", "x" * 201,
                                 BAD, None])
def test_replacement_key_must_be_nonreserved_nonempty_and_distinct(live, svc, monkeypatch, key):
    tid, _ = bad_card(live, svc, monkeypatch)
    assert_refused(live, svc, tid, "bad_replacement_key", key)


def test_replacement_key_must_be_unique_even_archived(live, svc, monkeypatch):
    tid, other = bad_card(live, svc, monkeypatch)
    quiet(live, "UPDATE tasks SET idempotency_key=?, status='archived' WHERE id=?", (GOOD, other))
    assert_refused(live, svc, tid, "replacement_key_in_use")


def test_unrelated_cards_and_rejections_are_untouched(live, svc, monkeypatch):
    tid, other = bad_card(live, svc, monkeypatch)
    bystander, _ = bad_card(live, svc, monkeypatch, key="pickup-stall:mini:disk:2", title="bystander")
    rows_other = [tuple(r) for r in live.execute("SELECT * FROM tasks WHERE id IN (?, ?)", (other, bystander))]
    bystander_ops = [dict(r) for r in live.execute("SELECT * FROM kanban_sync_outbox WHERE row_uid=?", (bystander,))]
    apply(live, svc, tid, preview(live, svc, tid)["token"])
    assert [tuple(r) for r in live.execute("SELECT * FROM tasks WHERE id IN (?, ?)", (other, bystander))] == rows_other
    assert [dict(r) for r in live.execute("SELECT * FROM kanban_sync_outbox WHERE row_uid=?", (bystander,))] == bystander_ops
    assert live.execute("SELECT COUNT(*) FROM kanban_sync_key_supersessions").fetchone()[0] == 1


# --- conservative resolution -----------------------------------------------------------------------

def test_without_receipt_old_key_op_never_matches_corrected_remote(live, svc, monkeypatch, kanban_home):
    tid, _ = bad_card(live, svc, monkeypatch)
    key_id = key_op(live)
    apply(live, svc, tid, preview(live, svc, tid)["token"])
    client.push(live, svc.hub(ME), BOARD, ME, primary=False)
    quiet(live, "DELETE FROM kanban_sync_key_supersession_ops")  # no verified mapping: only the old rules apply
    reports = resolve_rejections(live, svc.hub(ME), BOARD)
    task_op = next(r for r in reports if r["op_id"] == key_id)
    assert task_op["classification"] == "unresolved" and task_op["action"] == "retained"
    assert any(o["table_name"] == "tasks" for o in rejected_ops(live))


@pytest.mark.parametrize("tamper", ["state", "bytes", "remote_key", "local_key", "remote_owner"])
def test_receipt_resolution_requires_complete_untampered_receipt_and_remote_proof(live, svc, monkeypatch, tamper):
    tid, _ = bad_card(live, svc, monkeypatch)
    key_id = key_op(live)
    apply(live, svc, tid, preview(live, svc, tid)["token"])
    client.push(live, svc.hub(ME), BOARD, ME, primary=False)
    rk.advance(live, svc.hub(ME), BOARD)
    client.push(live, svc.hub(ME), BOARD, ME, primary=False)
    client._pull_replica(BOARD, svc.hub(ME), ME)
    assert rk.advance(live, svc.hub(ME), BOARD)[0].get("completed")  # the receipt is complete before it is tampered
    board_tasks = svc.board(BOARD)["rows"]["tasks"]
    tampers = {
        "state": lambda: quiet(live, "UPDATE kanban_sync_key_supersessions SET state='parent_queued'"),
        "bytes": lambda: quiet(live, "UPDATE kanban_sync_outbox SET last_error='edited' WHERE state='rejected' "
                                     "AND table_name='tasks'"),
        "remote_key": lambda: board_tasks[tid].__setitem__("idempotency_key", "somebody-else"),
        "local_key": lambda: quiet(live, "UPDATE tasks SET idempotency_key='local-edit' WHERE id=?", (tid,)),
        "remote_owner": lambda: board_tasks[tid].__setitem__("created_on", OTHER),
    }
    tampers[tamper]()
    reports = resolve_rejections(live, svc.hub(ME), BOARD)
    task_op = next(r for r in reports if r["op_id"] == key_id)
    assert task_op["classification"] == "unresolved", task_op


def test_receipt_resolution_retires_exactly_the_receipt_ops(live, svc, monkeypatch, kanban_home):
    tid, _ = bad_card(live, svc, monkeypatch)
    foreign = {"op_id": "foreign-op", "table_name": "tasks", "row_uid": tid, "op": "upsert",
               "fields": json.dumps({"idempotency_key": BAD}), "hlc": "", "created_at": 9, "state": "rejected",
               "last_error": "invalid", "attempts": 1}
    apply(live, svc, tid, preview(live, svc, tid)["token"])
    with kbc.write_txn(live):  # a rejected op the receipt does not name (appeared afterwards)
        live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at,state,last_error,"
                     "attempts) VALUES (:op_id,:table_name,:row_uid,:op,:fields,:hlc,:created_at,:state,:last_error,"
                     ":attempts)", foreign)
    rounds(live, svc, kanban_home)
    left = [o["op_id"] for o in rejected_ops(live)]
    assert left == ["foreign-op"]


# --- CLI ----------------------------------------------------------------------------------------------

def test_cli_preview_then_apply_requires_expect(live, svc, monkeypatch, kanban_home, capsys):
    from hermes_cli import kanban_sync_cli as cli
    tid, _ = bad_card(live, svc, monkeypatch)
    monkeypatch.setattr(cli, "_board", lambda args: BOARD)
    monkeypatch.setattr(cli, "_path", lambda slug: kanban_home / "kanban.db")
    monkeypatch.setattr(client, "load_identity", lambda: svc.hub(ME).ident)
    monkeypatch.setattr(client, "Hub", lambda ident: svc.hub(ME))
    args = Namespace(task_id=tid, new_key=GOOD, apply=False, expect=None, json=True, reason=REASON)
    assert cli._reserved_key(args) == 0
    token = json.loads(capsys.readouterr().out)["token"]
    assert live.execute("SELECT idempotency_key FROM tasks WHERE id=?", (tid,)).fetchone()[0] == BAD
    assert cli._reserved_key(Namespace(**{**vars(args), "apply": True})) == 2
    assert cli._reserved_key(Namespace(**{**vars(args), "apply": True, "expect": "wrong"})) == 1
    capsys.readouterr()
    assert cli._reserved_key(Namespace(**{**vars(args), "apply": True, "expect": token})) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "applied"
    assert live.execute("SELECT idempotency_key FROM tasks WHERE id=?", (tid,)).fetchone()[0] == GOOD
