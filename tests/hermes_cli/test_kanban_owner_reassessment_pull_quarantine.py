"""A contradicted reassessment receipt quarantines its own card in a real pull; siblings keep syncing."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_quarantine as quarantine
from tests.hermes_cli import kanban_quarantine_support as support
from tests.hermes_cli import test_kanban_containment_replica as fixtures
from tests.hermes_cli import test_kanban_owner_reassessment_canonical as canon

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


def _feed(monkeypatch, changes):
    page = {"changes": changes, "next": changes[-1].seq, "more": False}
    monkeypatch.setattr(ksc.acw, "changes_page", lambda *args: page)
    monkeypatch.setattr(ksc, "adapt", lambda change: change)


def test_reassessment_conflict_quarantines_exact_card_and_healthy_sibling_syncs(synced_conn, monkeypatch):
    conn = synced_conn
    bad_task = kb.create_task(conn, title="contradicted", assignee="w")
    sib_task = kb.create_task(conn, title="sibling", assignee="w")
    receipt = canon.make()
    receipt["task_id"] = bad_task
    park = receipt["park_event"]["uid"]
    hub = canon.Hub(canon.evidence_for(receipt))
    board = sync.sync_state(conn)["board_id"] if sync.sync_state(conn).get("board_id") else "default"
    receipt["board"] = board
    hub = canon.Hub({**canon.evidence_for(receipt), "board": board})
    # the feed row differs from the Board's attestation (created_at), for this one card only
    shaped = canon.ch(receipt, created_at=5)
    conflicting = sync.RemoteChange(1, "task_events", shaped.uid, "upsert", shaped.fields, 1, "", shaped.origin_gateway)
    sib_uid = sync.uuid7()
    sibling = sync.RemoteChange(2, "task_events", sib_uid, "upsert",
                                {"task_id": sib_task, "kind": "commented", "payload": "{}", "created_at": 1}, 2, "", "worker")
    _feed(monkeypatch, [conflicting, sibling])
    ksc.pull(conn, hub, board, observers=False)  # does NOT abort the round
    assert sync.sync_state(conn)["cursor"] == 2
    assert quarantine.tasks(conn) == {bad_task}
    assert support.count(conn, "containment_sync_quarantine") == 1
    row = conn.execute("SELECT uid,task_id,kind FROM containment_sync_quarantine").fetchone()
    assert tuple(row) == (park, bad_task, "reassessment_conflict")
    # fail closed: the held card does not dispatch; the sibling applied
    assert kb.claim_task(conn, bad_task) is None
    assert conn.execute("SELECT 1 FROM task_events WHERE uid=?", (sib_uid,)).fetchone() is not None
    assert conn.execute("SELECT 1 FROM task_events WHERE uid=?", (park,)).fetchone() is None  # withheld, not applied
    # durable: a later round re-reads nothing and the hold stays
    ksc.pull(conn, hub, board, observers=False)
    assert quarantine.tasks(conn) == {bad_task}
