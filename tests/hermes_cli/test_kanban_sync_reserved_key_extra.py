"""Candidate extra boundary tests for mutants that survived the existing three recovery files."""
import json

import pytest

from hermes_cli import kanban_sync_reserved_key as rk
from tests.hermes_cli import test_kanban_sync_reserved_key as base
from tests.hermes_cli import test_kanban_sync_reserved_key_boundaries as bnd
from tests.hermes_cli import test_kanban_sync_reserved_key_safety as safety

kanban_home = base.kanban_home
svc = base.svc
live = base.live


def test_preview_itself_rechecks_local_snapshot_after_evidence_read(live, svc, monkeypatch):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    hub = svc.hub(base.ME)
    original = hub.row_evidence
    calls = []

    def read(*a):
        out = original(*a)
        if not calls:
            calls.append(1)
            base.quiet(live, "UPDATE tasks SET status='running', claim_lock='w:1' WHERE id=?", (tid,))
        return out

    monkeypatch.setattr(hub, "row_evidence", read)
    report = rk.plan(live, hub, base.BOARD, tid, base.GOOD, base.REASON)
    assert calls
    assert report.get("refusal") == "local_state_changed_during_evidence_read", report
    assert not report.get("eligible") and not report.get("token")


def test_queue_requires_remote_parent_to_hold_corrected_key(live, svc, monkeypatch):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    safety.accepted_parent(live, svc, tid)
    svc.board(base.BOARD)["rows"]["tasks"][tid]["idempotency_key"] = "another-key"
    before = base.dump(live)
    report = rk.advance(live, svc.hub(base.ME), base.BOARD)[0]
    assert not report["advanced"] and report["waiting"] == "Board has not accepted the corrected card"
    bnd._assert_parent_waiting(live, tid)
    assert base.dump(live) == before


def test_delivery_proof_checks_row_uid_of_each_child(live, svc, monkeypatch):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    children = bnd._delivered_children(live, svc, tid)
    child = children[0]
    svc.board(base.BOARD)["rows"][child["table_name"]][child["row_uid"]]["uid"] = "someone-else"
    before = base.dump(live)
    report = rk.advance(live, svc.hub(base.ME), base.BOARD)[0]
    assert not report["advanced"], report
    bnd._assert_unfinished(live, tid)
    assert base.dump(live) == before


def test_task_op_retained_when_local_version_has_not_settled_at_board_version(live, svc, monkeypatch):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    task = next(o for o in base.rejected_ops(live) if o["op_id"] == base.key_op(live))
    safety.completed(live, svc, tid)
    remote_v = svc.board(base.BOARD)["rows"]["tasks"][tid]["version"]
    base.quiet(live, "UPDATE tasks SET sync_version=? WHERE id=?", (remote_v + 7, tid))
    bnd._assert_retained(live, svc, [task])


def test_second_rejected_task_op_with_other_old_key_is_not_retired(live, svc, monkeypatch):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    first = next(o for o in base.rejected_ops(live) if o["op_id"] == base.key_op(live))
    other_key = "pickup-stall:mini:disk:2"
    fields = json.loads(first["fields"])
    fields["idempotency_key"] = other_key
    extra = {**first, "op_id": "00-extra-other-key-op", "fields": json.dumps(fields)}
    bnd._reinsert(live, extra, "rejected")
    assert base.preview(live, svc, tid).get("eligible"), base.preview(live, svc, tid)
    safety.completed(live, svc, tid)
    extra_now = bnd._op(live, extra["op_id"])
    assert extra_now == extra
    reports = {r["op_id"]: r for r in base.resolve_rejections(live, svc.hub(base.ME), base.BOARD)}
    assert reports[extra["op_id"]]["classification"] == "unresolved", reports[extra["op_id"]]
    assert bnd._op(live, extra["op_id"]) == extra


def test_child_count_limit_is_inclusive(live, svc, monkeypatch):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    n = sum(len(v) for v in rk._snapshot(live, tid)["children"].values())
    assert n > 0
    monkeypatch.setattr(rk, "MAX_CHILDREN", n)
    assert base.preview(live, svc, tid).get("eligible")
    monkeypatch.setattr(rk, "MAX_CHILDREN", n - 1)
    assert "children" in (base.preview(live, svc, tid).get("refusal") or "")


def test_key_length_limit_is_inclusive(live, svc, monkeypatch):
    tid, _ = base.bad_card(live, svc, monkeypatch)
    assert base.preview(live, svc, tid, key="k" * rk.MAX_KEY_LEN).get("eligible")
    assert not base.preview(live, svc, tid, key="k" * (rk.MAX_KEY_LEN + 1)).get("eligible")
