"""Candidate tests for the remaining guarded-recovery mutation inventory (mutation-run2).

Each test names the mutant it kills; see mutation-run2/REPORT.md.
"""
import json
import time as _time
import types

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_sync_reserved_key as rk
from tests.hermes_cli import test_kanban_sync_reserved_key as base
from tests.hermes_cli import test_kanban_sync_reserved_key_boundaries as bnd
from tests.hermes_cli import test_kanban_sync_reserved_key_safety as safety

kanban_home = base.kanban_home
svc = base.svc
live = base.live


# --- _refusal -----------------------------------------------------------------------------------------

def test_lease_holder_alone_refuses_without_writes(live, svc, monkeypatch):
    """col_lease_holder: the existing lease case sets holder AND expiry together, so expiry masked a dropped holder."""
    tid, _ = base.bad_card(live, svc, monkeypatch)
    assert base.preview(live, svc, tid)["eligible"]
    base.quiet(live, "UPDATE tasks SET lease_holder='mini' WHERE id=?", (tid,))
    base.assert_refused(live, svc, tid, "active_run_or_lease")


@pytest.mark.parametrize("key", [123, b"ordinary-bytes-key"], ids=["int", "bytes"])
def test_replacement_key_must_be_a_string(live, svc, monkeypatch, key):
    """newkey_type: truthy, stripped, short, non-reserved non-strings are refused only by the isinstance clause."""
    tid, _ = base.bad_card(live, svc, monkeypatch)
    before = base.dump(live)
    report = base.preview(live, svc, tid, key)
    assert not report["eligible"] and report["refusal"] == "bad_replacement_key", report
    assert base.dump(live) == before


def test_child_without_uid_refused(live, svc, monkeypatch):
    """child_without_uid: a legacy NULL-uid child (written without capture triggers) cannot travel."""
    tid, _ = base.bad_card(live, svc, monkeypatch)
    assert base.preview(live, svc, tid)["eligible"]
    base.quiet(live, "UPDATE task_comments SET uid=NULL WHERE task_id=?", (tid,))
    before = base.dump(live)
    report = base.preview(live, svc, tid)
    assert not report["eligible"] and report["refusal"] == "child_without_uid", report
    assert base.apply(live, svc, tid, "x")["action"] == "refused" and base.dump(live) == before


@pytest.mark.parametrize("shape", ["key_removed", "other_key"])
def test_refuses_without_rejected_task_op_carrying_the_old_key(live, svc, monkeypatch, shape):
    """no_carrying_all / carrying_key: the rejected task op must itself carry the card's reserved key."""
    tid, _ = base.bad_card(live, svc, monkeypatch)
    op_id = base.key_op(live)
    fields = json.loads(live.execute("SELECT fields FROM kanban_sync_outbox WHERE op_id=?", (op_id,)).fetchone()[0])
    if shape == "key_removed":
        fields.pop("idempotency_key")
    else:
        fields["idempotency_key"] = "pickup-stall:mini:disk:99"
    base.quiet(live, "UPDATE kanban_sync_outbox SET fields=? WHERE op_id=?", (json.dumps(fields), op_id))
    before = base.dump(live)
    report = base.preview(live, svc, tid)
    assert not report["eligible"] and report["refusal"] == "no_rejected_op_carrying_the_key", report
    assert base.apply(live, svc, tid, "x")["action"] == "refused" and base.dump(live) == before


class _FrozenClock:
    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now

    def __getattr__(self, name):
        return getattr(_time, name)


def test_event_retention_cutoff_is_inclusive(live, svc, monkeypatch):
    """retention_cutoff: an event exactly at the service retention cutoff is still kept; one second older is not."""
    from hermes_cli.kanban_sync_client import EVENT_IMPORT_WINDOW_S
    tid, _ = base.bad_card(live, svc, monkeypatch)
    now = int(_time.time())
    monkeypatch.setattr(rk, "time", _FrozenClock(now))
    oldest = "UPDATE task_events SET created_at=? WHERE id=(SELECT MIN(id) FROM task_events WHERE task_id=?)"
    base.quiet(live, oldest, (now - EVENT_IMPORT_WINDOW_S, tid))
    assert base.preview(live, svc, tid)["eligible"], base.preview(live, svc, tid)
    base.quiet(live, oldest, (now - EVENT_IMPORT_WINDOW_S - 1, tid))
    report = base.preview(live, svc, tid)
    assert not report["eligible"] and report["refusal"].startswith("unsupported_old_event"), report


# --- creation ordering ---------------------------------------------------------------------------------

def test_reserved_key_refused_before_existing_card_lookup(live, svc, monkeypatch):
    """create_lookup_first: an ordinary create must raise even when a card already holds that reserved key."""
    _tid, _ = base.bad_card(live, svc, monkeypatch, push=False)
    before = base.dump(live)
    with pytest.raises(ValueError, match="reserved for the pickup-stall detector"):
        kb.create_task(live, title="again", assignee="worker", created_by="user", idempotency_key=base.BAD)
    assert base.dump(live) == before


# --- snapshot envelope: one clause at a time --------------------------------------------------------------

class _Cursor:
    """A non-string cursor that a bare ``nxt <= after`` would happily accept."""

    def __le__(self, other):
        return False


@pytest.mark.parametrize("page_factory", [
    lambda: types.MappingProxyType({"head": 1, "table": "tasks", "rows": [], "more": False}),
    lambda: {"head": 1, "table": "tasks", "rows": {}, "more": False},
    lambda: {"head": 1, "table": "tasks", "rows": [types.MappingProxyType({"uid": "a"})], "more": False},
    lambda: {"head": 1, "table": "tasks", "rows": [], "more": 0},
], ids=["page_not_dict", "rows_not_list", "row_not_dict", "more_not_bool"])
def test_snapshot_envelope_each_clause_refuses(live, svc, monkeypatch, page_factory):
    """snap_page_dict / snap_rows_list / snap_row_dict / snap_more_bool: shapes that only one clause rejects."""
    tid, _ = base.bad_card(live, svc, monkeypatch)
    hub = svc.hub(base.ME)
    monkeypatch.setattr(hub, "snapshot_page", lambda *a, **k: page_factory())
    report = rk.plan(live, hub, base.BOARD, tid, base.GOOD, base.REASON)
    assert not report["eligible"], report
    assert report["refusal"] == "authoritative_snapshot_incomplete: malformed page", report


def test_snapshot_next_cursor_must_be_a_string(live, svc, monkeypatch):
    """snap_next_str: a non-string cursor refuses; it must not be accepted and followed to a clean second page."""
    tid, _ = base.bad_card(live, svc, monkeypatch)
    hub = svc.hub(base.ME)
    pages = [{"head": 1, "table": "tasks", "rows": [], "more": True, "next": _Cursor()}, {"head": 1, "table": "tasks", "rows": [], "more": False}]
    monkeypatch.setattr(hub, "snapshot_page", lambda *a, **k: pages.pop(0))
    report = rk.plan(live, hub, base.BOARD, tid, base.GOOD, base.REASON)
    assert not report["eligible"], report
    assert report["refusal"] == "authoritative_snapshot_incomplete: page cursor does not advance", report


# --- queue / finish: receipt state revalidated inside the writer transaction ------------------------------------

def _settle_receipt_externally(live, tid):
    with base.kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_key_supersessions SET state='complete', completed_at=123 WHERE task_id=?", (tid,))


def test_queue_step_vetoes_receipt_state_change_inside_transaction(live, svc, monkeypatch):
    """queue_state: another advancer completes the receipt after our parent evidence read; do not re-queue children."""
    tid, _ = base.bad_card(live, svc, monkeypatch)
    safety.accepted_parent(live, svc, tid)
    hub = svc.hub(base.ME)
    original, hits = hub.row_evidence, []

    def read(*args):
        evidence = original(*args)
        if not hits:
            hits.append(1)
            _settle_receipt_externally(live, tid)
        return evidence

    monkeypatch.setattr(hub, "row_evidence", read)
    report = rk.advance(live, hub, base.BOARD)[0]
    assert hits and not report["advanced"] and "changed during evidence read" in report["waiting"], report
    receipt = rk.receipt_for_task(live, tid)
    assert receipt["state"] == "complete" and receipt["completed_at"] == 123
    assert live.execute("SELECT count(*) FROM kanban_sync_key_supersession_children").fetchone()[0] == 0


def test_finish_step_vetoes_receipt_state_change_inside_transaction(live, svc, monkeypatch):
    """finish_state: another advancer completes the receipt during delivery evidence; do not overwrite completed_at."""
    tid, _ = base.bad_card(live, svc, monkeypatch)
    bnd._delivered_children(live, svc, tid)
    hub = svc.hub(base.ME)
    original, hits = hub.row_evidence, []

    def read(*args):
        evidence = original(*args)
        if not hits:
            hits.append(1)
            _settle_receipt_externally(live, tid)
        return evidence

    monkeypatch.setattr(hub, "row_evidence", read)
    report = rk.advance(live, hub, base.BOARD)[0]
    assert hits and not report["advanced"] and "changed during evidence read" in report["waiting"], report
    receipt = rk.receipt_for_task(live, tid)
    assert receipt["state"] == "complete" and receipt["completed_at"] == 123


def test_queue_step_vetoes_card_removed_inside_transaction(live, svc, monkeypatch):
    """queue_task_none: the corrected card vanishes after the parent evidence read; waiting, not a crash."""
    tid, _ = base.bad_card(live, svc, monkeypatch)
    safety.accepted_parent(live, svc, tid)
    hub = svc.hub(base.ME)
    original, hits = hub.row_evidence, []

    def read(*args):
        evidence = original(*args)
        if not hits:
            hits.append(1)
            base.quiet(live, "DELETE FROM tasks WHERE id=?", (tid,))
        return evidence

    monkeypatch.setattr(hub, "row_evidence", read)
    report = rk.advance(live, hub, base.BOARD)[0]
    assert hits and not report["advanced"], report
    assert report["waiting"] in ("receipt changed during evidence read", "receipt or identity changed during evidence read")
    assert live.execute("SELECT count(*) FROM kanban_sync_key_supersession_children").fetchone()[0] == 0
