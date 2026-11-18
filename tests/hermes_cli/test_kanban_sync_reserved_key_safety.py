"""Safety gates of the reserved-key correction: identity binding, reason, full evidence, child delivery, uniqueness,
evidence interval (kanban_sync_reserved_key)."""
import argparse
import contextlib
import dataclasses
import json
from argparse import Namespace

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_sync_reserved_key as rk
from hermes_cli.kanban_sync_rejections import resolve_rejections
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli import test_kanban_sync_reserved_key as base
from tests.hermes_cli.test_kanban_sync_reserved_key import (
    BOARD, GOOD, ME, OTHER, REASON, apply, bad_card, dump, key_op, preview, quiet, rejected_ops)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return base.StrictService()


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    client.ensure_identity(conn, BOARD, ME)
    try:
        yield conn
    finally:
        conn.close()


class HubView:
    """The same Board connection presented with another enrolled identity (gateway / endpoint)."""

    def __init__(self, hub, **ident):
        self._hub, self.ident = hub, dataclasses.replace(hub.ident, **ident)

    def __getattr__(self, name):
        return getattr(self._hub, name)


def plan_with(live, hub, tid, key=GOOD, reason=REASON):
    return rk.plan(live, hub, BOARD, tid, key, reason)


def accepted_parent(live, svc, tid):
    """Apply, send the corrected parent, and let the local card learn the Board's version."""
    token = preview(live, svc, tid)["token"]
    assert apply(live, svc, tid, token)["action"] == "applied"
    client.push(live, svc.hub(ME), BOARD, ME, primary=False)
    client._pull_replica(BOARD, svc.hub(ME), ME)
    return token


@pytest.mark.parametrize("conflict", ["missing", "tombstone", "pending"])
def test_child_conflict_cannot_complete_partial_manifest(live, svc, monkeypatch, conflict):
    tid, _ = bad_card(live, svc, monkeypatch)
    accepted_parent(live, svc, tid)
    uid = live.execute("SELECT uid FROM task_comments WHERE task_id=? LIMIT 1", (tid,)).fetchone()[0]
    if conflict == "missing":
        quiet(live, "DELETE FROM task_comments WHERE uid=?", (uid,))
    elif conflict == "tombstone":
        quiet(live, "INSERT INTO kanban_sync_tombstones VALUES ('task_comments',?,1)", (uid,))
    else:
        quiet(live, "INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) "
              "VALUES ('conflict','task_comments',?,'upsert','{}','',1)", (uid,))
    rk.advance(live, svc.hub(ME), BOARD)
    assert rk.receipt_for_task(live, tid)["state"] == "parent_queued"
    assert live.execute("SELECT count(*) FROM kanban_sync_key_supersession_children").fetchone()[0] == 0


@pytest.mark.parametrize("stage", ["parent", "children"])
def test_transient_retry_keeps_replacement_op_custody(live, svc, monkeypatch, stage):
    tid, _ = bad_card(live, svc, monkeypatch)
    if stage == "parent":
        apply(live, svc, tid, preview(live, svc, tid)["token"])
    else:
        accepted_parent(live, svc, tid)
        rk.advance(live, svc.hub(ME), BOARD)
    hub = svc.hub(ME)
    monkeypatch.setattr(hub, "ops", lambda board, ops: {"results": [
        {"client_op_id": op["client_op_id"], "status": "rejected", "reason": "storage_error"} for op in ops]})
    old = {r[0] for r in live.execute("SELECT op_id FROM kanban_sync_outbox WHERE state='pending'")}
    client.push(live, hub, BOARD, ME, primary=False)
    pending = {r[0] for r in live.execute("SELECT op_id FROM kanban_sync_outbox WHERE state='pending'")}
    assert pending and not pending.intersection(old)
    receipt = rk.receipt_for_task(live, tid)
    tracked = ({receipt["parent_op_id"]} if stage == "parent" else {r[0] for r in live.execute(
        "SELECT op_id FROM kanban_sync_key_supersession_children")})
    assert tracked == pending


@pytest.mark.parametrize("token,reason", [(None, REASON), ("stale", REASON), ("valid", "different reason")])
def test_repeat_apply_still_requires_expected_intent(live, svc, monkeypatch, token, reason):
    tid, _ = bad_card(live, svc, monkeypatch)
    original = accepted_parent(live, svc, tid)
    before = dump(live)
    result = rk.apply(live, svc.hub(ME), BOARD, tid, GOOD, original if token == "valid" else token, reason)
    assert result["action"] == "refused"
    assert dump(live) == before


def test_recovery_never_synthesizes_other_link_endpoint(live, svc, monkeypatch):
    tid, other = bad_card(live, svc, monkeypatch)
    accepted_parent(live, svc, tid)
    rk.advance(live, svc.hub(ME), BOARD)
    quiet(live, "UPDATE tasks SET sync_version=NULL WHERE id=?", (other,))
    rows = [dict(r) for r in live.execute("SELECT * FROM kanban_sync_outbox WHERE state='pending'")]
    assert rows
    assert client._unacked_parents(live, rows, ME) == []


@pytest.mark.parametrize("field", ["priority", "workspace_kind"])
def test_recovery_refuses_compatibility_downgrades(live, svc, monkeypatch, field):
    tid, _ = bad_card(live, svc, monkeypatch)
    apply(live, svc, tid, preview(live, svc, tid)["token"])
    hub = svc.hub(ME)
    monkeypatch.setattr(hub, "ops", lambda board, ops: {"results": [
        {"client_op_id": op["client_op_id"], "status": "rejected", "reason": "forbidden_field",
         "detail": f"not writable on tasks: {field}"} for op in ops]})
    client.push(live, hub, BOARD, ME, primary=False, max_batches=1)
    assert live.execute("SELECT count(*) FROM kanban_sync_omissions").fetchone()[0] == 0
    receipt = rk.receipt_for_task(live, tid)
    assert live.execute("SELECT state FROM kanban_sync_outbox WHERE op_id=?",
                        (receipt["parent_op_id"],)).fetchone()[0] == "rejected"


# --- 1: strict hub identity, board identity, endpoint binding -------------------------------------------

@pytest.mark.parametrize("gateway", [None, "", "   ", OTHER])
def test_hub_gateway_must_be_present_and_equal(live, svc, monkeypatch, gateway):
    tid, _ = bad_card(live, svc, monkeypatch)
    before = dump(live)
    report = plan_with(live, HubView(svc.hub(ME), gateway=gateway), tid)
    assert not report["eligible"] and report["refusal"] == "foreign_gateway_identity"
    result = rk.apply(live, HubView(svc.hub(ME), gateway=gateway), BOARD, tid, GOOD, "x", REASON)
    assert result["action"] == "refused" and dump(live) == before


def test_hub_without_identity_refused(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)

    class Bare:
        def __getattr__(self, name):
            if name == "ident":
                raise AttributeError(name)
            return getattr(svc.hub(ME), name)
    assert plan_with(live, Bare(), tid)["refusal"] == "foreign_gateway_identity"


def test_state_board_id_must_equal_requested_board(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    quiet(live, "UPDATE kanban_sync_state SET board_id='another-board'")
    report = plan_with(live, svc.hub(ME), tid)
    assert not report["eligible"] and report["refusal"] == "foreign_board_identity"


def test_token_binds_endpoint_and_gateway(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    first = plan_with(live, svc.hub(ME), tid)["token"]
    assert plan_with(live, svc.hub(ME), tid)["token"] == first  # deterministic
    moved = HubView(svc.hub(ME), url="https://other-endpoint.test")
    assert plan_with(live, moved, tid)["token"] != first
    before = dump(live)
    result = rk.apply(live, moved, BOARD, tid, GOOD, first, REASON)
    assert result["action"] == "refused" and result["refusal"] == "stale_or_missing_expected_token"
    assert dump(live) == before
    receipt = apply(live, svc, tid, first) and rk.receipt_for_task(live, tid)
    assert receipt["endpoint"] and "key" not in receipt["endpoint"] and receipt["gateway_id"] == ME


def test_token_does_not_leak_or_depend_on_secret_key(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    first = plan_with(live, svc.hub(ME), tid)["token"]
    assert plan_with(live, HubView(svc.hub(ME), key="another-secret"), tid)["token"] == first


@pytest.mark.parametrize("change", [{"url": "https://elsewhere.test"}, {"gateway": OTHER}])
def test_advance_revalidates_identity_and_endpoint(live, svc, monkeypatch, change):
    tid, _ = bad_card(live, svc, monkeypatch)
    accepted_parent(live, svc, tid)
    waiting = rk.advance(live, HubView(svc.hub(ME), **change), BOARD)[0]
    assert not waiting["advanced"] and waiting["waiting"].startswith("identity or endpoint differs")
    assert rk.receipt_for_task(live, tid)["state"] == "parent_queued"
    assert rk.advance(live, svc.hub(ME), BOARD)[0]["advanced"]


def test_advance_vetoes_identity_change_inside_transaction(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    accepted_parent(live, svc, tid)
    hub = svc.hub(ME)
    original = hub.row_evidence

    def read(*a):
        out = original(*a)
        quiet(live, "UPDATE kanban_sync_state SET board_id='moved-board'")
        return out

    monkeypatch.setattr(hub, "row_evidence", read)
    report = rk.advance(live, hub, BOARD)[0]
    assert not report["advanced"]
    assert rk.receipt_for_task(live, tid)["state"] == "parent_queued"
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE state='pending'").fetchone()


# --- 2: reason -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("reason", [None, "", "   \n", 5])
def test_reason_required_for_preview_and_apply(live, svc, monkeypatch, reason):
    tid, _ = bad_card(live, svc, monkeypatch)
    token = preview(live, svc, tid)["token"]
    before = dump(live)
    report = plan_with(live, svc.hub(ME), tid, reason=reason)
    assert not report["eligible"] and report["refusal"] == "reason_required"
    result = rk.apply(live, svc.hub(ME), BOARD, tid, GOOD, token, reason)
    assert result["action"] == "refused" and result["refusal"] == "reason_required" and dump(live) == before


def test_reason_is_bound_to_token_and_persisted_trimmed(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    token = preview(live, svc, tid)["token"]
    assert plan_with(live, svc.hub(ME), tid, reason="a different reason")["token"] != token
    before = dump(live)
    stale = rk.apply(live, svc.hub(ME), BOARD, tid, GOOD, token, "a different reason")
    assert stale["refusal"] == "stale_or_missing_expected_token" and dump(live) == before
    assert rk.apply(live, svc.hub(ME), BOARD, tid, GOOD, token, f"  {REASON}  ")["action"] == "applied"
    assert rk.receipt_for_task(live, tid)["reason"] == REASON


@pytest.mark.parametrize("flags", [[], ["--reason", "  "], ["--apply", "--expect", "t"],
                                   ["--apply", "--expect", "t", "--reason", ""]])
def test_cli_reason_is_required_and_nonblank(live, svc, monkeypatch, kanban_home, capsys, flags):
    from hermes_cli import kanban as kc
    from hermes_cli import kanban_sync_cli as cli
    tid, _ = bad_card(live, svc, monkeypatch)
    monkeypatch.setattr(cli, "_board", lambda args: BOARD)
    monkeypatch.setattr(cli, "_path", lambda slug: kanban_home / "kanban.db")
    monkeypatch.setattr(client, "load_identity", lambda: svc.hub(ME).ident)
    monkeypatch.setattr(client, "Hub", lambda ident: svc.hub(ME))
    parser = argparse.ArgumentParser()
    kc.build_parser(parser.add_subparsers(dest="cmd"))
    before = dump(live)
    try:
        args = parser.parse_args(["kanban", "sync", "reserved-key", tid, "--new-key", GOOD, *flags])
    except SystemExit as exc:  # argparse: the flag is required
        assert exc.code == 2
    else:
        assert cli._reserved_key(args) == 2
    assert dump(live) == before


def test_cli_reason_reaches_receipt(live, svc, monkeypatch, kanban_home, capsys):
    from hermes_cli import kanban_sync_cli as cli
    tid, _ = bad_card(live, svc, monkeypatch)
    monkeypatch.setattr(cli, "_board", lambda args: BOARD)
    monkeypatch.setattr(cli, "_path", lambda slug: kanban_home / "kanban.db")
    monkeypatch.setattr(client, "load_identity", lambda: svc.hub(ME).ident)
    monkeypatch.setattr(client, "Hub", lambda ident: svc.hub(ME))
    args = Namespace(task_id=tid, new_key=GOOD, apply=False, expect=None, json=True, reason="because")
    assert cli._reserved_key(args) == 0
    token = json.loads(capsys.readouterr().out)["token"]
    assert cli._reserved_key(Namespace(**{**vars(args), "apply": True, "expect": token})) == 0
    assert rk.receipt_for_task(live, tid)["reason"] == "because"


# --- 3: every identity is evidenced; unsupported shapes fail closed ------------------------------------

def test_evidence_covers_every_identity_exactly(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    hub, seen = svc.hub(ME), []
    original = hub.row_evidence
    monkeypatch.setattr(hub, "row_evidence", lambda b, t, u: seen.append((t, u)) or original(b, t, u))
    report = plan_with(live, hub, tid)
    assert report["eligible"]
    expected = {("tasks", tid)} | {tuple(c) for c in report["children"]}
    events = [r[0] for r in live.execute("SELECT uid FROM task_events WHERE task_id=?", (tid,))]
    assert events and {("task_events", e) for e in events} <= expected
    assert sorted(seen) == sorted(expected) and report["evidence_checked"] == len(expected)


def test_old_event_fails_closed_instead_of_being_dropped(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    quiet(live, "UPDATE task_events SET created_at=1 WHERE id=(SELECT MIN(id) FROM task_events WHERE task_id=?)", (tid,))
    before = dump(live)
    report = plan_with(live, svc.hub(ME), tid)
    assert not report["eligible"] and report["refusal"].startswith("unsupported_old_event")
    assert apply(live, svc, tid, "x")["action"] == "refused" and dump(live) == before


def test_fresh_events_remain_supported(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    assert any(c[0] == "task_events" for c in plan_with(live, svc.hub(ME), tid)["children"])


def test_outgoing_link_fails_closed(live, svc, monkeypatch):
    tid, other = bad_card(live, svc, monkeypatch)
    quiet(live, "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (tid, other))
    report = plan_with(live, svc.hub(ME), tid)
    assert not report["eligible"] and report["refusal"].startswith("unsupported_outgoing_link")


@pytest.mark.parametrize("shape", ["delete", "release", "ghost", "deleted_identity", "other_op"])
def test_unaccounted_or_unsupported_rejected_ops_refuse(live, svc, monkeypatch, shape):
    tid, _ = bad_card(live, svc, monkeypatch)
    comment = live.execute("SELECT uid FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0]
    row = {"table": "task_comments", "uid": comment, "op": "upsert", "fields": {"task_id": tid}}
    shapes = {"delete": {"op": "delete"},
              "release": {"table": "tasks", "uid": tid, "op": "release", "fields": {"run_uid": "r"}},
              "ghost": {"uid": "ghost-comment"}, "other_op": {"op": "compat_import"}, "deleted_identity": {}}
    row.update(shapes[shape])
    if shape != "deleted_identity":
        with kbc.write_txn(live):
            live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at,state,"
                         "last_error,attempts) VALUES ('x1',?,?,?,?,'',3,'rejected','invalid',1)",
                         (row["table"], row["uid"], row["op"], json.dumps(row["fields"])))
    else:  # the identity the rejected op names is gone locally: nothing can carry it
        quiet(live, "DELETE FROM task_comments WHERE uid=?", (comment,))
        with kbc.write_txn(live):
            live.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at,state,"
                         "last_error,attempts) VALUES ('x1','task_comments',?,'upsert',?,'',3,'rejected','invalid',1)",
                         (comment, json.dumps({"task_id": tid})))
    before = dump(live)
    report = plan_with(live, svc.hub(ME), tid)
    assert not report["eligible"] and report["refusal"].split(":")[0] in (
        "unsupported_outbox_op", "unaccounted_rejection"), report
    assert apply(live, svc, tid, "x")["action"] == "refused" and dump(live) == before


# --- 4: child delivery tracked; proof-bound retirement -------------------------------------------------

def test_children_are_queued_then_delivered_before_complete(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    accepted_parent(live, svc, tid)
    report = rk.advance(live, svc.hub(ME), BOARD)[0]
    assert report["advanced"] and report["children_queued"] > 0
    receipt = rk.receipt_for_task(live, tid)
    assert receipt["state"] == "children_queued" and receipt["completed_at"] is None
    pending = {r[0] for r in live.execute("SELECT op_id FROM kanban_sync_outbox WHERE state='pending'")}
    recorded = {r[0] for r in live.execute("SELECT op_id FROM kanban_sync_key_supersession_children")}
    assert pending == recorded and len(recorded) == report["children_queued"]
    assert "queued" in rk.advance(live, svc.hub(ME), BOARD)[0]["waiting"]  # still undelivered
    assert rk.receipt_for_task(live, tid)["state"] == "children_queued"
    client.push(live, svc.hub(ME), BOARD, ME, primary=False)
    done = rk.advance(live, svc.hub(ME), BOARD)[0]
    assert done["advanced"] and rk.receipt_for_task(live, tid)["state"] == "complete"
    assert {r[0] for r in live.execute("SELECT op_id FROM kanban_sync_key_supersession_children")} == recorded


def test_rejected_child_op_blocks_completion(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    accepted_parent(live, svc, tid)
    rk.advance(live, svc.hub(ME), BOARD)
    quiet(live, "UPDATE kanban_sync_outbox SET state='rejected', last_error='invalid' WHERE state='pending' "
                "AND table_name='task_comments'")
    report = rk.advance(live, svc.hub(ME), BOARD)[0]
    assert not report["advanced"] and "rejected" in report["waiting"]
    assert rk.receipt_for_task(live, tid)["state"] == "children_queued"


def test_vanished_child_op_without_delivery_is_not_complete(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    accepted_parent(live, svc, tid)
    rk.advance(live, svc.hub(ME), BOARD)
    quiet(live, "DELETE FROM kanban_sync_outbox WHERE state='pending' AND table_name='task_comments'")
    client.push(live, svc.hub(ME), BOARD, ME, primary=False)  # every other child reaches the Board
    assert "delivered" in rk.advance(live, svc.hub(ME), BOARD)[0]["waiting"]  # the Board never saw that child
    assert rk.receipt_for_task(live, tid)["state"] == "children_queued"


def completed(live, svc, tid):
    accepted_parent(live, svc, tid)
    rk.advance(live, svc.hub(ME), BOARD)
    client.push(live, svc.hub(ME), BOARD, ME, primary=False)
    rk.advance(live, svc.hub(ME), BOARD)
    assert rk.receipt_for_task(live, tid)["state"] == "complete"
    client._pull_replica(BOARD, svc.hub(ME), ME)


def classes(live, svc):
    return {r["op_id"]: r["classification"] for r in resolve_rejections(live, svc.hub(ME), BOARD)}


def test_complete_receipt_retires_every_old_op_with_matching_rows(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    old = [o["op_id"] for o in rejected_ops(live)]
    completed(live, svc, tid)
    assert set(old) == {k for k, v in classes(live, svc).items() if v != "unresolved"}


def test_task_op_with_remote_field_difference_is_not_retired(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    key_id = key_op(live)
    completed(live, svc, tid)
    svc.board(BOARD)["rows"]["tasks"][tid]["title"] = "somebody else's title"
    assert classes(live, svc)[key_id] == "unresolved"
    assert any(o["op_id"] == key_id for o in rejected_ops(live))


def test_task_op_with_local_field_difference_is_not_retired(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    key_id = key_op(live)
    completed(live, svc, tid)
    quiet(live, "UPDATE tasks SET title='locally edited later' WHERE id=?", (tid,))
    assert classes(live, svc)[key_id] == "unresolved"


def test_child_op_uses_conservative_equality_even_with_receipt(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    comment = next(o for o in rejected_ops(live) if o["table_name"] == "task_comments")
    completed(live, svc, tid)
    svc.board(BOARD)["rows"]["task_comments"][comment["row_uid"]]["body"] = "different on the Board"
    assert classes(live, svc)[comment["op_id"]] == "unresolved"


@pytest.mark.parametrize("tamper", ["state_children_queued", "state_parent_queued", "bytes", "task_id", "old_key",
                                    "gateway"])
def test_receipt_covered_op_never_falls_back_to_conservative_rules(live, svc, monkeypatch, tamper):
    tid, _ = bad_card(live, svc, monkeypatch)
    comment = next(o for o in rejected_ops(live) if o["table_name"] == "task_comments")
    completed(live, svc, tid)
    sql = {"state_children_queued": "UPDATE kanban_sync_key_supersessions SET state='children_queued'",
           "state_parent_queued": "UPDATE kanban_sync_key_supersessions SET state='parent_queued'",
           "bytes": f"UPDATE kanban_sync_outbox SET last_error='edited' WHERE op_id='{comment['op_id']}'",
           "task_id": "UPDATE kanban_sync_key_supersessions SET task_id='someone-else'",
           "old_key": "UPDATE kanban_sync_key_supersessions SET old_key='pickup-stall:x:y:9'",
           "gateway": "UPDATE kanban_sync_key_supersessions SET gateway_id='mini'"}[tamper]
    quiet(live, sql)
    result = classes(live, svc)
    covered = {o["op_id"] for o in rejected_ops(live)}
    assert covered and comment["op_id"] in covered
    # without the receipt gate the (equal) comment op would have been retired by the conservative rules
    assert result[comment["op_id"]] == "unresolved", tamper


def test_receipt_covered_op_with_remote_task_not_owned_by_gateway_not_retired(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    comment = next(o for o in rejected_ops(live) if o["table_name"] == "task_comments")
    completed(live, svc, tid)
    svc.board(BOARD)["rows"]["tasks"][tid]["created_on"] = OTHER
    assert classes(live, svc)[comment["op_id"]] == "unresolved"


# --- 5: replacement-key uniqueness against the Board ---------------------------------------------------

def test_remote_duplicate_key_refused(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    svc.board(BOARD)["rows"].setdefault("tasks", {})["someone"] = {"uid": "someone", "version": 1,
                                                                  "idempotency_key": GOOD, "created_on": OTHER}
    before = dump(live)
    report = plan_with(live, svc.hub(ME), tid)
    assert not report["eligible"] and report["refusal"] == "replacement_key_in_remote_use"
    assert apply(live, svc, tid, "x")["action"] == "refused" and dump(live) == before


def test_remote_snapshot_is_authenticated_exhaustive_and_paged(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    hub, calls = svc.hub(ME), []
    pages = {"": {"head": 1, "table": "tasks", "rows": [{"uid": "a", "idempotency_key": "x"}], "next": "a", "more": True},
             "a": {"head": 1, "table": "tasks", "rows": [{"uid": "b", "idempotency_key": GOOD}], "next": "b", "more": False}}
    monkeypatch.setattr(hub, "snapshot_page", lambda board, table, after="", *a: calls.append((board, table, after))
                        or pages[after])
    report = plan_with(live, hub, tid)
    assert calls == [(BOARD, "tasks", ""), (BOARD, "tasks", "a")]
    assert report["refusal"] == "replacement_key_in_remote_use"


@pytest.mark.parametrize("page", [
    {"head": 1, "rows": [], "next": "", "more": True},          # claims more but cannot advance
    {"head": 1, "rows": []},                                      # no completeness flag
    {"head": 1, "rows": "nope", "more": False},                   # malformed
    {"rows": [], "more": False},                                  # no head
    {"head": 1, "rows": [{"uid": "a"}], "more": True, "next": "a"},  # never ends (cap)
    {"head": 1, "rows": [], "more": "no", "next": ""},            # non-boolean flag
])
def test_incomplete_or_malformed_snapshot_refuses(live, svc, monkeypatch, page):
    tid, _ = bad_card(live, svc, monkeypatch)
    monkeypatch.setattr(rk, "MAX_SNAPSHOT_PAGES", 3)
    hub = svc.hub(ME)
    monkeypatch.setattr(hub, "snapshot_page", lambda *a, **k: page)
    report = plan_with(live, hub, tid)
    assert not report["eligible"] and report["refusal"].startswith("authoritative_snapshot_incomplete"), report


@pytest.mark.parametrize("exc", [client.HubError(404, "no route"), client.SyncUnavailable("down"), OSError("net")])
def test_snapshot_failure_refuses(live, svc, monkeypatch, exc):
    tid, _ = bad_card(live, svc, monkeypatch)
    hub = svc.hub(ME)

    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(hub, "snapshot_page", boom)
    report = plan_with(live, hub, tid)
    assert not report["eligible"] and report["refusal"].startswith("authoritative_snapshot_unavailable")


def test_hub_without_snapshot_route_refuses(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)

    class NoSnapshot:
        ident = svc.hub(ME).ident

        def row_evidence(self, *a):
            return svc.hub(ME).row_evidence(*a)
    report = plan_with(live, NoSnapshot(), tid)
    assert not report["eligible"] and report["refusal"].startswith("authoritative_snapshot_unavailable")


def test_snapshot_cursor_must_advance(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    hub = svc.hub(ME)
    monkeypatch.setattr(hub, "snapshot_page", lambda *a, **k: {"head": 1, "rows": [], "next": "", "more": True})
    assert "cursor" in plan_with(live, hub, tid)["refusal"]


def test_snapshot_page_cap_is_incomplete_even_when_cursor_advances(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    monkeypatch.setattr(rk, "MAX_SNAPSHOT_PAGES", 3)
    hub = svc.hub(ME)
    monkeypatch.setattr(hub, "snapshot_page", lambda b, t, after="", *a: {
        "head": 1, "rows": [{"uid": after + "a"}], "next": after + "a", "more": True})
    assert "page cap" in plan_with(live, hub, tid)["refusal"]


def test_preview_itself_enforces_evidence_interval(live, svc, monkeypatch, clock):
    tid, _ = bad_card(live, svc, monkeypatch)
    hub = svc.hub(ME)
    original = hub.snapshot_page

    def slow(*a, **k):
        clock.now += rk.EVIDENCE_MAX_AGE_S + 1
        return original(*a, **k)
    monkeypatch.setattr(hub, "snapshot_page", slow)
    report = plan_with(live, hub, tid)
    assert not report["eligible"] and report["refusal"] == "evidence_interval_exceeded"


def test_complete_receipt_bound_to_task_of_the_op(live, svc, monkeypatch):
    tid, _ = bad_card(live, svc, monkeypatch)
    key_id = key_op(live)
    completed(live, svc, tid)
    quiet(live, "UPDATE kanban_sync_key_supersessions SET task_id='someone-else'")
    assert classes(live, svc)[key_id] == "unresolved"


# --- 6: evidence interval --------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(rk, "_mono", c)
    return c


def slow_lock(monkeypatch, clock, seconds):
    real = rk.write_txn

    @contextlib.contextmanager
    def slow(conn):
        with real(conn):
            clock.now += seconds  # the writer lock wait counts toward the evidence age
            yield
    monkeypatch.setattr(rk, "write_txn", slow)


def test_evidence_interval_includes_writer_lock_wait(live, svc, monkeypatch, clock):
    tid, _ = bad_card(live, svc, monkeypatch)
    token = preview(live, svc, tid)["token"]
    slow_lock(monkeypatch, clock, rk.EVIDENCE_MAX_AGE_S + 0.01)
    before = dump(live)
    result = apply(live, svc, tid, token)
    assert result["action"] == "refused" and result["refusal"] == "evidence_interval_exceeded"
    assert dump(live) == before


def test_evidence_interval_boundary_is_inclusive(live, svc, monkeypatch, clock):
    tid, _ = bad_card(live, svc, monkeypatch)
    token = preview(live, svc, tid)["token"]
    slow_lock(monkeypatch, clock, rk.EVIDENCE_MAX_AGE_S)
    assert apply(live, svc, tid, token)["action"] == "applied"


@pytest.mark.parametrize("call", ["row_evidence", "snapshot_page"])
def test_evidence_interval_covers_every_remote_read(live, svc, monkeypatch, clock, call):
    tid, _ = bad_card(live, svc, monkeypatch)
    token = preview(live, svc, tid)["token"]
    hub = svc.hub(ME)
    original = getattr(hub, call)

    def slow(*a, **k):
        clock.now += rk.EVIDENCE_MAX_AGE_S + 1
        return original(*a, **k)
    monkeypatch.setattr(hub, call, slow)
    result = rk.apply(live, hub, BOARD, tid, GOOD, token, REASON)
    assert result["action"] == "refused" and result["refusal"] == "evidence_interval_exceeded"
    assert rk.receipt_for_task(live, tid) is None


def test_clock_going_backwards_fails_closed(live, svc, monkeypatch, clock):
    tid, _ = bad_card(live, svc, monkeypatch)
    token = preview(live, svc, tid)["token"]
    slow_lock(monkeypatch, clock, -5)
    assert apply(live, svc, tid, token)["refusal"] == "evidence_interval_exceeded"


def test_wall_clock_jump_does_not_affect_interval(live, svc, monkeypatch, clock):
    tid, _ = bad_card(live, svc, monkeypatch)
    token = preview(live, svc, tid)["token"]
    import time as time_module
    real = time_module.time
    monkeypatch.setattr(rk.time, "time", lambda: real() + 10 ** 6)  # NTP step: only the monotonic clock counts
    assert apply(live, svc, tid, token)["action"] == "applied"


def test_advance_evidence_interval_vetoes_inside_transaction(live, svc, monkeypatch, clock):
    tid, _ = bad_card(live, svc, monkeypatch)
    accepted_parent(live, svc, tid)
    slow_lock(monkeypatch, clock, rk.EVIDENCE_MAX_AGE_S + 1)
    report = rk.advance(live, svc.hub(ME), BOARD)[0]
    assert not report["advanced"] and "evidence_interval_exceeded" in report["waiting"]
    assert rk.receipt_for_task(live, tid)["state"] == "parent_queued"
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE state='pending'").fetchone()
