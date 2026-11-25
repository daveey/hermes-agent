"""Review v11: an over-cap park evidence holds only its card; a generic release is never advertised for it."""
from __future__ import annotations

import logging

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_quarantine as quarantine
from hermes_cli import kanban_sync_quarantine_release as qr
from hermes_cli import kanban_sync_hub_transport as transport
BoundedResponseError = transport.BoundedResponseError
# On the reviewed head this is the existing cap exception, so RED reaches pull/logging rather than import.
BoundedResponseTooLarge = getattr(transport, "BoundedResponseTooLarge", BoundedResponseError)
from tests.hermes_cli import kanban_quarantine_support as support
from tests.hermes_cli import test_kanban_containment_replica as fixtures
from tests.hermes_cli import test_kanban_owner_reassessment_canonical as canon
from tests.hermes_cli.test_kanban_owner_reassessment_pull_quarantine import _feed

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


class _Hub(canon.Hub):
    """Raises ``error`` for the park's evidence (a Board answer over the raw cap), serves nothing else."""

    def __init__(self, error, ident_hub):
        super().__init__({}, gateway=ident_hub.ident.gateway)
        self.error = error

    def row_evidence(self, board, table, uid, *, max_response_bytes):
        assert max_response_bytes == canon.canonical.MAX_EVIDENCE_BYTES  # the raw wire cap is never raised
        self.calls += 1
        raise self.error


def _scenario(conn, monkeypatch, error):
    bad_task = kb.create_task(conn, title="big park", assignee="w")
    sib_task = kb.create_task(conn, title="sibling", assignee="w")
    receipt = canon.make()
    receipt["task_id"] = bad_task
    board = sync.sync_state(conn)["board_id"] if sync.sync_state(conn).get("board_id") else "default"
    receipt["board"] = board
    park = receipt["park_event"]["uid"]
    hub = _Hub(error, canon.Hub({}))
    shaped = canon.ch(receipt)
    bad = sync.RemoteChange(1, "task_events", shaped.uid, "upsert", shaped.fields, 1, "", shaped.origin_gateway)
    sib_uid = sync.uuid7()
    sibling = sync.RemoteChange(2, "task_events", sib_uid, "upsert",
                                {"task_id": sib_task, "kind": "commented", "payload": "{}", "created_at": 1}, 2, "", "worker")
    _feed(monkeypatch, [bad, sibling])
    return bad_task, sib_task, park, sib_uid, board, hub


def test_over_cap_park_evidence_holds_that_card_and_healthy_sibling_syncs(synced_conn, monkeypatch):
    conn = synced_conn
    bad_task, _sib, park, sib_uid, board, hub = _scenario(
        conn, monkeypatch, BoundedResponseTooLarge("Board response exceeds decoded byte limit"))
    ksc.pull(conn, hub, board, observers=False)  # RED: BoundedResponseError aborted the whole pull
    assert sync.sync_state(conn)["cursor"] == 2
    row = conn.execute("SELECT uid,task_id,kind FROM containment_sync_quarantine").fetchone()
    assert tuple(row) == (park, bad_task, quarantine.REASSESSMENT_OVERSIZE)
    assert quarantine.tasks(conn) == {bad_task}
    # no authority: no receipt stored, park withheld, card cannot be claimed; the sibling applied
    assert support.count(conn, "owner_reassessment_canonical_receipts") == 0
    assert conn.execute("SELECT 1 FROM task_events WHERE uid=?", (park,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM task_events WHERE uid=?", (sib_uid,)).fetchone() is not None
    assert kb.claim_task(conn, bad_task) is None
    ksc.pull(conn, hub, board, observers=False)  # the fake feed re-serves the page: still held, one row, no authority
    assert quarantine.tasks(conn) == {bad_task} and support.count(conn, "containment_sync_quarantine") == 1
    assert support.count(conn, "owner_reassessment_canonical_receipts") == 0


@pytest.mark.parametrize("error", [BoundedResponseError("Invalid compressed Board response"), OSError("reset"),
                                   TimeoutError("slow")])
def test_other_evidence_failures_are_not_deterministic_conflicts(synced_conn, monkeypatch, error):
    conn = synced_conn
    _bad, _sib, _park, sib_uid, board, hub = _scenario(conn, monkeypatch, error)
    with pytest.raises((BoundedResponseError, OSError)):
        ksc.pull(conn, hub, board, observers=False)
    assert sync.sync_state(conn)["cursor"] == 0
    assert support.count(conn, "containment_sync_quarantine") == 0
    assert conn.execute("SELECT 1 FROM task_events WHERE uid=?", (sib_uid,)).fetchone() is None


@pytest.mark.parametrize("kind", ["conflict", "oversize"])
def test_release_is_not_advertised_and_an_attempt_stays_fail_closed(synced_conn, monkeypatch, caplog, kind):
    conn = synced_conn
    if kind == "oversize":
        error = BoundedResponseTooLarge("Board response exceeds wire byte limit")
    else:
        error = None
    bad_task, _sib, park, _sib_uid, board, hub = _scenario(conn, monkeypatch, error or BoundedResponseError("x"))
    if kind == "conflict":
        receipt = canon.make()
        receipt["task_id"], receipt["board"] = bad_task, board
        park = receipt["park_event"]["uid"]
        hub = canon.Hub({**canon.evidence_for(receipt), "board": board})
        shaped = canon.ch(receipt, created_at=5)
        bad = sync.RemoteChange(1, "task_events", shaped.uid, "upsert", shaped.fields, 1, "", shaped.origin_gateway)
        _feed(monkeypatch, [bad])
    with caplog.at_level(logging.ERROR):
        ksc.pull(conn, hub, board, observers=False)
    want = "reassessment_conflict" if kind == "conflict" else quarantine.REASSESSMENT_OVERSIZE
    assert conn.execute("SELECT kind FROM containment_sync_quarantine WHERE uid=?", (park,)).fetchone()[0] == want
    # the operator-facing text never names the always-refusing generic release as the way out
    texts = [r.getMessage() for r in caplog.records if "quarantined receipt" in r.getMessage()]
    assert texts and all("--release" not in t.replace("generic `sync quarantine --release` does NOT", "") for t in texts)
    assert all("explicitly" in t for t in texts)
    held = quarantine.refusal(conn, bad_task)
    assert "does NOT clear" in held and "adjudicates" in held and "--reason" not in held
    from hermes_cli import kanban_sync_quarantine_notice as notice
    body = notice._body(quarantine.entries(conn)[0])
    assert "--reason" not in body and "adjudicates" in body and park in body
    # an actual release attempt refuses, stores no receipt, keeps the hold and the card unclaimable
    with pytest.raises(qr.ReleaseRefused):
        qr.release(conn, hub, board, park, reason="try", operator="test")
    assert park in quarantine.receipts(conn) and quarantine.tasks(conn) == {bad_task}
    assert support.count(conn, "owner_reassessment_canonical_receipts") == 0
    assert conn.execute("SELECT 1 FROM task_events WHERE uid=?", (park,)).fetchone() is None
    assert kb.claim_task(conn, bad_task) is None


def test_other_kinds_still_advertise_generic_release():
    text = quarantine.clear_instruction("identity_changed", "b", "r1")
    assert "sync quarantine --release r1" in text
